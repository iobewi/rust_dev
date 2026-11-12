"""P4-ROM-MD5 (G1) - the MD5 of the same 4096 bytes at 0x2000 on the ESP32-P4, computed by the ROM loader WITHOUT the flasher stub. READ ONLY: no write, no erase, no reset
command, no flash read (the P4 ROM has no read-flash command in esptool: only SPI_FLASH_MD5).

Why: PC-7 (FAIL) and PC-8 (FAIL at the identification of the second command) used the stub. For a flash larger than 16 MiB the stub reads through its own 4-byte fast read
(ROM exec_cmd function), not through the ROM's read routine. The ROM computes SPI_FLASH_MD5 with its own routine. If the ROM MD5 of the same range is stable while the stub's
varies, suspicion moves to the stub's read path; if it varies too, a cause common to both paths is suspected. This scenario takes only the ROM measurements.

The agent is asked explicitly for `loader: "rom"` (return_console=false does NOT mean no stub): the stub upload is skipped on that loader instance, the ECO7 flash power
sequence of the connection still runs, the baud rate stays the ROM one, the flash is attached with the 8-byte SPI_ATTACH payload of esptool. The identity check of the agent
stays strict (JEDEC id read again for every command; a failure keeps the raw value and its rank, nothing is bypassed). Proof that no stub ran, per measurement, from four sources that do not all depend on the loader flag: the chip's own answer is the ROM
format (`rom-ascii32`, 32 ASCII characters: a stub would send 16 raw bytes and esptool-js would reject the shorter answer), the host sent no memory-load command
(`ram_load_ops` 0: ESP_MEM_BEGIN/END/DATA counted on the wire), and the two flags derived from `IS_STUB` agree (`loader` is `rom`, `loader_stub` 0). A stub already running
is also refused from the chip's sync answers. Anything else stops the scenario.

Sequence: 1) ROM MD5, return_console=false (first command of a session, rank 1); 2) ROM MD5, return_console=true (second command of the SAME session, rank 2; the console returns);
3) only if 1 and 2 agree: ROM MD5 after a NEW loader entry (rank 1), return_console=true. No retry, no replay, one run.

Preconditions: consent `P4-ROM-MD5` written by the operator, assets digest equal to `integrity.expected_assets_sha256` (the agent that has the ROM mode), `flash.md5` announced, P4 / 32 MiB,
persistent evidence. Range fixed: 0x2000 / 4096 (below 16 MiB: the ROM does not fully address beyond).

Criteria, written before any run:
  PASS           measurements 1, 2 and 3 completed with the ROM format and no stub, identical MD5, ranks 1, 2, 1 (row ROM-A). It says that THIS observation of the ROM path was stable;
  FAIL           an operation failed (the raw JEDEC id and rank are kept), a measurement was not a ROM answer without stub, or the MD5 differ (rows ROM-B / ROM-C are hypotheses);
  INDETERMINATE  an operation is unresolved or the proof window broke: nothing is replayed;
  BLOCKED        no consent, not the P4 / 32 MiB, no declared assets digest, another agent version, no `flash.md5` capability, evidence not persistent.
A PASS does NOT authorise PC-BACKUP and does not say which bytes differ elsewhere. Hardware status: NOT executed.
"""
from importlib.metadata import version

import pytest

from iobewi_bench.client import OperationError

from .backup import free_space_problem
from .evidence import record_storage, require_persistent_evidence
from .harness import Blocked, Outcome, make_campaign, recorded
from .pc import first_command, gate, loader_state, open_loader_window      # noqa: F401  (loader_state is a fixture)
from .readonly import _console_after_early_exit, record_transport, start_or_block
from .rom_md5 import analyse_rom, summary
from .session import build_manifest as campaign_manifest
from .test_pc_integrity import ADDRESS, P4_CHIP, P4_FLASH, SIZE, _failure, sync_evidence

pytestmark = pytest.mark.bench
SCENARIO = "P4-ROM-MD5"


def test_pc9_p4_rom_md5(bench_client, qual_session, pytestconfig, loader_state, request):
    cfg = qual_session
    gate(cfg, SCENARIO)
    if cfg["chip"] != P4_CHIP or cfg["flash_size"] != P4_FLASH:
        pytest.skip(f"BLOCKED: this recipe measures the {P4_CHIP} with {P4_FLASH} bytes; the session declares {cfg['chip']} / {cfg['flash_size']}")
    expected = cfg["integrity"].get("expected_assets_sha256")
    if not expected:
        pytest.skip("BLOCKED: integrity.expected_assets_sha256 is not declared: the agent with the ROM mode cannot be verified")
    storage = require_persistent_evidence(cfg, pytestconfig)
    problem = free_space_problem(cfg["evidence_root"], 1024 * 1024)
    if problem:
        pytest.skip("BLOCKED: " + problem)
    manifest = campaign_manifest(cfg, SCENARIO, regions=[], steps=["Loader entry WITHOUT stub may restart the P4 (ROM MD5 twice in one session, then once after a new entry)"])
    case = make_campaign(bench_client, cfg, "PC-9", __file__, pytestconfig, manifest=manifest,
                         parameters={"address": ADDRESS, "size": SIZE, "expected_assets_sha256": expected, "loader": "rom"})
    request.addfinalizer(lambda: sync_evidence(case.directory))
    out = Outcome()
    with recorded(case, "PC-9", out, preconditions=f"{cfg['chip']} / {cfg['transport']}, agent assets {expected[:12]}",
                  limits="Read only; ROM MD5 of one range of 4 KiB; the loader entry may restart the card; MD5 only (no bytes); the answer is carried by the same serial link; "
                         "no cause is established; one run; says nothing about the stub except by comparison with earlier runs"):
        start_or_block(case)
        out.evidence.append("evidence storage: " + record_storage(case, storage))
        record_transport(case, bench_client, cfg, out)
        device = bench_client.device(cfg["board_id"])
        provenance = device.get("provenance") or {}
        health = bench_client.health()
        case.write("versions.json", {"server": health.get("build"), "epoch_prefix": health["epoch"][:8], "agent_sha256": provenance.get("agent_sha256"),
                                    "page_assets_sha256": provenance.get("assets_sha256"), "expected_assets_sha256": expected,
                                    "client": version("iobewi-bench"), "generation_prefix": device["generation"][:8]})
        if provenance.get("assets_sha256") != expected:
            raise Blocked(f"the agent reports page assets {str(provenance.get('assets_sha256'))[:12]}, the session expects {expected[:12]}: "
                          "not the reviewed agent; no command was submitted")
        if "flash.md5" not in (device.get("capabilities") or []):
            raise Blocked("the card does not announce the flash.md5 capability; no command was submitted")
        out.evidence.append(f"agent page assets {expected[:12]} as declared; flash.md5 announced")
        board = case.reserve(ttl=900)
        open_loader_window(case, board, cfg, out, loader_state)
        observations = []
        try:
            for index, (return_console, session) in enumerate(((False, "first"), (True, "same-session"), (True, "new-entry"))):
                name = f"{index}-rom_md5"
                args = {"address": ADDRESS, "size": SIZE, "return_console": return_console, "loader": "rom"}
                try:
                    call = lambda: board.command("flash.md5", args)      # noqa: E731
                    result = first_command(cfg, loader_state, call) if index == 0 else call()
                except OperationError as error:
                    _failure(case, name, error, out)                    # raw JEDEC id, rank and stub flag of a failed identification are kept
                    raise
                identify = result.get("identify") or {}
                record = {"kind": "rom_md5", "index": index, "session": session, "operation": board.last_operation["id"], "md5": result.get("md5"),
                          "format": result.get("format"), "loader": result.get("loader"), "ram_load_ops": result.get("ram_load_ops"), "identify": identify}
                observations.append(record)
                case.write(f"{name}.json", record)
                out.evidence.append(f"{name} ({session}): md5={record['md5']} format={record['format']} loader={record['loader']} "
                                    f"jedec={identify.get('jedec_id')} rank={identify.get('identify_index')} loader_stub={identify.get('loader_stub')} ram_load_ops={record['ram_load_ops']}")
                assert record["format"] == "rom-ascii32" and record["loader"] == "rom" and identify.get("loader_stub") == 0 \
                    and record["ram_load_ops"] == 0, \
                    f"{name}: the answer is not a ROM answer without stub (format={record['format']}, loader={record['loader']}, loader_stub={identify.get('loader_stub')}, ram_load_ops={record['ram_load_ops']})"
                if index == 1 and record["md5"] != observations[0]["md5"]:
                    break                                                # the third measurement is taken only if the first two agree
        except BaseException as error:
            _console_after_early_exit(board, out, error)
            raise
        ranks = [(o["identify"] or {}).get("identify_index") for o in observations]
        analysis = analyse_rom(observations)
        case.write("rom-analysis.json", analysis)
        out.evidence.append(f"identification ranks: {ranks}; " + summary(analysis))
        for finding in analysis["findings"]:
            out.evidence.append(f"{finding['level']}: {finding['text']}")
        out.evidence.append(analysis["caution"])
        out.evidence.append("Scope: this scenario does not authorise the full P4 backup (PC-BACKUP) and compares nothing byte by byte")
        assert analysis["conclusion"] == "consistent", f"ROM MD5 stability not established: {summary(analysis)}"
        assert ranks == [1, 2, 1], f"the session structure was not as planned (identification ranks {ranks})"
