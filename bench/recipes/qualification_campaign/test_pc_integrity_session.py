"""P4-FLASH-INTEGRITY-SESSION (G1) - the same 4096 bytes at 0x2000 on the ESP32-P4, measured inside ONE loader session. READ ONLY: no write, no erase, no reset.

Why: PC-7 (run v1, FAIL) took its five measurements in five different loader sessions (every command returned the console, so each one reset the card, loaded
the stub, initialised the flash controller again and read the JEDEC id again). It showed variation between the chip MD5s and between the reads, but cannot
tell whether the variation exists inside one session or only appears between two initialisations. This scenario keeps the loader (return_console=false) and
takes: chip MD5, read (+ the MD5 the stub sends after the data), chip MD5. Same address and size as PC-7. Nothing else changes.

The identity check of the agent stays strict: it reads the JEDEC id again for every command, also inside a session (no cache, no relaxation, no retry). In the
first hardware run a second command in the same session failed in the `identify` phase; the agent now reports the raw JEDEC id and the rank of the
identification, so that failure, if it happens again, is itself the measurement and is kept as it is (the sequence then stops).

Preconditions: those of PC-7 (consent `P4-FLASH-INTEGRITY-SESSION` written by the operator, assets digest equal to `integrity.expected_assets_sha256`, `flash.md5`
announced, P4 / 32 MiB, persistent evidence). The first command of the sequence is the chip MD5 itself (`identify` takes no argument and always returns the
console, so it cannot open a session that stays). Proof that the session was continuous: the identification ranks reported by the three commands are 1, 2, 3 and no
console-opened event is seen before the explicit console return at the end.

Criteria, written before any run:
  PASS           three operations completed in one session (ranks 1, 2, 3, no console event in between), every digest present and valid, all MD5 and all received
                 bytes agree (row R4). It says that THIS observation, inside one session, was consistent; it does not clear the variation seen between sessions;
  FAIL           an operation failed definitively (the raw JEDEC id is kept), a digest is missing / partial / invalid, the measurements disagree, or the session was
                 not continuous;
  INDETERMINATE  an operation is unresolved or the proof window broke: nothing is replayed;
  BLOCKED        no consent, not the P4 / 32 MiB, no declared assets digest, another agent version, no `flash.md5` capability, evidence not persistent.
One explicit console return ends the scenario (it may restart the firmware). A PASS does NOT authorise PC-BACKUP. Hardware status: NOT executed.
"""
import hashlib
from importlib.metadata import version

import pytest

from iobewi_bench.client import OperationError

from .backup import free_space_problem
from .evidence import record_storage, require_persistent_evidence
from .harness import Blocked, Outcome, make_campaign, recorded
from .integrity import PLAN_SESSION, analyse, summary
from .pc import first_command, gate, loader_state, open_loader_window      # noqa: F401  (loader_state is a fixture)
from .readonly import console_on_exit, record_transport, retained_reads, start_or_block
from .session import build_manifest as campaign_manifest
from .test_pc_integrity import ADDRESS, P4_CHIP, P4_FLASH, SIZE, _failure, durable_copy, sync_evidence

pytestmark = pytest.mark.bench
SCENARIO = "P4-FLASH-INTEGRITY-SESSION"


def test_pc8_p4_flash_integrity_one_session(bench_client, qual_session, pytestconfig, loader_state, request):
    cfg = qual_session
    gate(cfg, SCENARIO)
    if cfg["chip"] != P4_CHIP or cfg["flash_size"] != P4_FLASH:
        pytest.skip(f"BLOCKED: this recipe reads the {P4_CHIP} with {P4_FLASH} bytes; the session declares {cfg['chip']} / {cfg['flash_size']}")
    expected = cfg["integrity"].get("expected_assets_sha256")
    if not expected:
        pytest.skip("BLOCKED: integrity.expected_assets_sha256 is not declared: the instrumented agent cannot be verified")
    storage = require_persistent_evidence(cfg, pytestconfig)
    problem = free_space_problem(cfg["evidence_root"], 1024 * 1024)
    if problem:
        pytest.skip("BLOCKED: " + problem)
    manifest = campaign_manifest(cfg, SCENARIO, regions=[], steps=["Loader entry may restart the P4 (chip MD5, read, chip MD5 in ONE loader session, then one console return)"])
    case = make_campaign(bench_client, cfg, "PC-8", __file__, pytestconfig, manifest=manifest,
                         parameters={"address": ADDRESS, "size": SIZE, "expected_assets_sha256": expected, "plan": list(PLAN_SESSION), "one_loader_session": True})
    request.addfinalizer(lambda: sync_evidence(case.directory))
    out = Outcome()
    with recorded(case, "PC-8", out, preconditions=f"{cfg['chip']} / {cfg['transport']}, agent assets {expected[:12]}",
                  limits="Read only; one range of 4 KiB; 2 chip MD5 and 1 read in one loader session; the loader entry may restart the card; digests are what the agent "
                         "reported over the same serial link; no cause is established; one run; a PASS does not clear the variation seen between sessions"):
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
        cursor = board.cursor()
        evidence_dir = case.directory / "board-0"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        observations, ranks = [], []
        with console_on_exit(board, out), retained_reads(board, out) as reads:       # one explicit console return at the end; readbacks released after verification
            for index, kind in enumerate(PLAN_SESSION):
                name = f"{index}-{kind}"
                try:
                    if kind == "chip_md5":
                        call = lambda: board.command("flash.md5", {"address": ADDRESS, "size": SIZE, "return_console": False})      # noqa: E731
                        result = first_command(cfg, loader_state, call) if index == 0 else call()
                        record = {"kind": kind, "index": index, "operation": board.last_operation["id"], "md5": result.get("md5"), "format": result.get("format"),
                                  "identify": result.get("identify")}
                        out.evidence.append(f"{name}: chip md5={record['md5']} format={record['format']}")
                    else:
                        result = board.command("flash.read", {"address": ADDRESS, "size": SIZE, "return_console": False, "stub_digest": True})
                        artifact = result["artifact"]
                        reads.held.append(dict(artifact))
                        data = board.client.download(artifact)
                        durable_copy(evidence_dir, index, ADDRESS, data, artifact, board.last_operation["id"])
                        digests = result.get("stub_digests") or []
                        stub = digests[0] if len(digests) == 1 else {"state": "missing" if not digests else "unexpected-count", "count": len(digests)}
                        record = {"kind": kind, "index": index, "operation": board.last_operation["id"], "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                  "md5_client": hashlib.md5(data).hexdigest(), "stub": stub, "identify": result.get("identify")}
                        assert len(data) == SIZE and result.get("address") == ADDRESS, f"{name}: unexpected address or size"
                        out.evidence.append(f"{name}: sha256={record['sha256']} md5_client={record['md5_client']} stub={stub.get('state')} stub_md5={stub.get('md5')}")
                except OperationError as error:
                    _failure(case, name, error, out)                       # raw JEDEC id, rank and stub flag of a failed identification are kept
                    raise
                info = record.get("identify") or {}
                ranks.append(info.get("identify_index"))
                observations.append(record)
                case.write(f"{name}.json", record)
            consoles = [e for e in board.events(cursor) if e["kind"] == "transport" and "console opened" in (e.get("detail") or "")]
            out.evidence.append(f"identification ranks in the loader session: {ranks}; console-opened events before the explicit return: {len(consoles)}")
            analysis = analyse(observations, PLAN_SESSION)
            case.write("integrity-analysis.json", analysis)
            out.evidence.append(summary(analysis))
            for finding in analysis["findings"]:
                out.evidence.append(f"{finding['level']}: {finding['text']}")
            out.evidence.append(analysis["caution"])
            out.evidence.append("Scope: this scenario does not authorise the full P4 backup (PC-BACKUP), which keeps its own consent, evidence storage check and review; "
                                "a PASS inside one session does not clear the variation seen between sessions (PC-7 v1)")
            assert ranks == [1, 2, 3], f"the loader session was not continuous (identification ranks {ranks})"
            assert not consoles, f"{len(consoles)} console-opened event(s) inside the session: not one continuous loader session"
            assert analysis["conclusion"] == "consistent", f"integrity not established: {summary(analysis)}"
