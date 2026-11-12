"""PC-BACKUP (G1) - complete, verified backup of the ESP32-P4 flash (33 554 432 bytes) through the bench. READ ONLY: no write, no erase, no reset.

Target: ONE card, the ESP32-P4 with 32 MiB. Nothing about the C6 is covered. Every read is a normal bench `flash.read` (one artifact per segment);
Orchestrator.backup does the durable local copy (written, fsynced, re-read) BEFORE the server artifact is deleted, so a failure never loses the
only copy. The segments are then verified against a manifest (no missing, duplicated, shifted or altered segment), assembled into one image,
and the global SHA-256 is computed from the files re-read on disk.

Criteria, written before any run:
  PASS           all segments of the exact plan were read, each copy re-read equal, the manifest verifies, the assembled image equals the
                 concatenation and holds 33 554 432 bytes, the reservation is released, Campaign.close() succeeded;
  FAIL           a read failed definitively, a size / digest / address check failed, a local copy could not be verified, or the storage is full;
  INDETERMINATE  an operation or submission is unresolved, or the proof window broke (epoch, generation, disconnection): nothing is replayed,
                 the segments already secured stay in the private evidence folder;
  BLOCKED        no consent / not the P4 / not 32 MiB / evidence storage ephemeral / not enough free space / loader not reachable.
A backup is a sequence of observations of a card that may change between reads (consistency: sequential observations), not an atomic snapshot.
Loader entry may restart the card (once per segment when return_console is true). Hardware status: NOT executed.
"""
import contextlib
import json

import pytest

from iobewi_bench import Orchestrator

from .backup import (BackupError, assemble, build_manifest, free_space_problem, plan_segments, segment_name, verify_backup)
from .evidence import record_storage, require_persistent_evidence
from .harness import Blocked, Outcome, make_campaign, recorded
from .pc import first_command, gate, loader_state, open_loader_window      # noqa: F401  (loader_state is a fixture)
from .readonly import console_on_exit, record_transport, start_or_block
from .session import build_manifest as campaign_manifest

pytestmark = pytest.mark.bench
P4_CHIP, P4_FLASH = "esp32p4", 33554432
HEADROOM = 256 * 1024 * 1024      # the segment copies of Board.read_flash, the backup files and the assembled image: about three times the flash


def test_pc5_full_p4_backup(bench_client, qual_session, pytestconfig, loader_state):
    cfg = qual_session
    gate(cfg, "PC-BACKUP")
    if cfg["chip"] != P4_CHIP or cfg["flash_size"] != P4_FLASH:
        pytest.skip(f"BLOCKED: this recipe backs up the {P4_CHIP} with {P4_FLASH} bytes; the session declares {cfg['chip']} / {cfg['flash_size']}")
    storage = require_persistent_evidence(cfg, pytestconfig)
    problem = free_space_problem(cfg["evidence_root"], 3 * P4_FLASH + HEADROOM)
    if problem:
        pytest.skip("BLOCKED: " + problem)
    segment, back_to_console, timeout = cfg["backup"]["segment_size"], cfg["backup"]["return_console"], cfg["operation_timeout_s"]
    plan = plan_segments(P4_FLASH, segment)
    manifest = campaign_manifest(cfg, "PC-BACKUP", regions=[], steps=["Loader entry may restart the card once per segment when backup.return_console is true"])
    case = make_campaign(bench_client, cfg, "PC-5", __file__, pytestconfig, manifest=manifest,
                         parameters={"flash_size": P4_FLASH, "segment_size": segment, "segments": len(plan), "return_console": back_to_console,
                                     "timeout": timeout})
    runner, out = Orchestrator(case), Outcome()
    with recorded(case, "PC-5", out, preconditions=f"{cfg['chip']} / {cfg['transport']} / loader {cfg['loader']['entry']}, {len(plan)} segment(s) of {segment} bytes",
                  limits="Read only; sequential observations, not an atomic snapshot; the card may restart on each loader entry; says nothing about "
                         "the firmware stored, only that its bytes were read, copied and verified; one run"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        out.evidence.append("evidence storage: " + record_storage(case, storage))
        board = case.reserve(ttl=3600)
        open_loader_window(case, board, cfg, out, loader_state)
        identity = first_command(cfg, loader_state, board.identify)
        case.write("identity.json", {k: identity.get(k) for k in ("chip", "flash_size", "flash_id")})        # never the MAC
        assert identity["chip"] == P4_CHIP, f"measured {identity['chip']}, expected {P4_CHIP}"
        assert identity["flash_size"] == P4_FLASH, f"measured {identity['flash_size']}, expected {P4_FLASH}"
        directory = board.output_dir
        rows = []
        with (contextlib.nullcontext() if back_to_console else console_on_exit(board, out)):
            for index, address, size in plan:
                board.renew(ttl=3600)                                   # explicit, before every segment; never implicit
                path, meta = runner.backup(board, address=address, size=size, filename=segment_name(index), segment_size=size,
                                           timeout=timeout, return_console=back_to_console)
                assert meta["complete"] is True and (meta["address"], meta["size"]) == (address, size), f"segment {index}: unexpected metadata"
                rows.append({"index": index, "address": address, "size": size, "sha256": meta["sha256"],
                             "operation": meta["segments"][0]["operation"], "file": path.name})
                out.evidence.append(f"segment {index} {address:#x}+{size:#x} sha256={meta['sha256']}")
        document = build_manifest(directory, P4_FLASH, segment, rows, epoch=board.epoch, generation=board.generation, device=cfg["board_id"])
        try:
            verify_backup(directory, document, expected_total=P4_FLASH)           # re-reads every file from disk
            document["global_sha256"] = assemble(directory, document)
            document["assembled"] = "full-flash.bin"
            verified = verify_backup(directory, document, expected_total=P4_FLASH)
        except BackupError as error:
            raise AssertionError("backup verification failed: " + str(error)) from error
        (directory / "backup-manifest.json").write_text(json.dumps(document, indent=2))
        # Descriptor in the format of Orchestrator.backup: the assembled image can then be compared (and, with a later mandate, restored)
        # by the existing primitives, without a parallel mechanism.
        (directory / "full-flash.json").write_text(json.dumps({"address": 0, "size": P4_FLASH, "sha256": verified, "complete": True,
                                                              "consistency": "sequential_observations", "segments": rows}, indent=2))
        out.evidence.append(f"manifest verified: {len(rows)} segments, {P4_FLASH} bytes, global sha256={verified}")
        out.restoration = "not applicable (no mutation); backup kept in the private evidence folder"
