"""P4-FLASH-INTEGRITY (G1) - is a read of 4096 bytes at 0x2000 on the ESP32-P4 the same data that the chip holds? READ ONLY: no write, no erase, no reset.

Why: the run p4-jedec-read-v1 produced two different SHA-256 for the same range (and a third in the first run). Nothing says whether the flash, the stub or the
serial transfer is responsible. This recipe adds three independent measurements of the SAME range, taken in this order, each opening a fresh loader session
(return_console=true):
    identify, md5 (chip, SPI_FLASH_MD5), read (+ the MD5 the stub sends after the data), md5, read (+ stub MD5), md5.
The client computes the MD5 and the SHA-256 of what it received. `integrity.analyse` compares everything and names, at most, hypotheses.

Preconditions: the agent's page assets digest equals `integrity.expected_assets_sha256` (reviewed and deployed by the operator); the card is the ESP32-P4 with
32 MiB and announces the `flash.md5` capability; consent `P4-FLASH-INTEGRITY` in the private session, written by the operator. No retry, no replay.

Criteria, written before any run:
  PASS           all five operations completed, every digest present and valid, all MD5 and all received bytes agree (analysis row R4), no change of
                 target / generation / epoch, reservation released. It says the integrity of THIS observation, nothing more;
  FAIL           a command failed definitively, a digest is missing / partial / invalid, or the measurements disagree (the rows R1-R3 are hypotheses);
  INDETERMINATE  an operation is unresolved or the proof window broke: nothing is replayed;
  BLOCKED        no consent, not the P4 / 32 MiB, no declared assets digest, another agent version, no `flash.md5` capability.
A PASS does NOT authorise the full backup of the P4 (PC-BACKUP) by itself: PC-BACKUP keeps its own operator consent, its own persistent evidence storage
and free-space checks, and the review of its commit. The PASS only says that this observation of this 4 KiB range was consistent.

Evidence and artifacts: the evidence root must be persistent (BLOCKED otherwise, as for PC-BACKUP / PC-CYCLE; the development override is recorded). Every received
read is copied to a private file (0600, exclusive creation, fsync of file and directory, re-read) BEFORE any comparison; the server artifact is deleted only after
that copy was re-read and matched (ReadbackRetention), is kept when the copy cannot be verified or an operation is unresolved, and a failed or indeterminate
run never deletes a readback. At the end every evidence file and directory of the run is fsynced.
Hardware status: NOT executed.
"""
import hashlib
import json
import os
from importlib.metadata import version

import pytest

from iobewi_bench.client import OperationError

from .backup import free_space_problem
from .evidence import record_storage, require_persistent_evidence
from .harness import Blocked, Outcome, make_campaign, recorded
from .integrity import PLAN, analyse, summary
from .pc import first_command, gate, loader_state, open_loader_window      # noqa: F401  (loader_state is a fixture)
from .readonly import record_transport, retained_reads, start_or_block
from .session import build_manifest as campaign_manifest

pytestmark = pytest.mark.bench
P4_CHIP, P4_FLASH = "esp32p4", 33554432
ADDRESS, SIZE = 0x2000, 4096


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_copy(directory, index, address, data, artifact, operation):
    """Private copy of a received read, kept BEFORE any comparison and before any server cleanup. Same name and sidecar as Board.read_flash
    (flash-NNN-<addr>.bin/.json), so that the existing retention check (ReadbackRetention.local_copy_matches) can verify it. Exclusive creation
    (never over existing evidence), mode 0600, file and directory fsynced, bytes re-read and compared."""
    path = directory / f"flash-{index:03d}-{address:08x}.bin"
    with open(path, "xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    sidecar = path.with_suffix(".json")
    with open(sidecar, "x") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({"address": address, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "artifact": artifact["id"], "operation": operation}, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(directory)
    if path.read_bytes() != data:
        raise AssertionError(f"private copy {path.name} differs from the bytes received")
    return path


def sync_evidence(root):
    """Best effort at the end of the scenario, also after a failure: flush every evidence file and directory of the run to stable storage."""
    for folder, _, names in os.walk(root):
        for name in names:
            try:
                descriptor = os.open(os.path.join(folder, name), os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                pass
        try:
            _sync_directory(folder)
        except OSError:
            pass


def _failure(case, name, error, out):
    failure = error.operation.get("error") or {}
    case.write(f"{name}-failure.json", {"operation": error.operation.get("id"), "state": error.operation.get("state"), "error": failure})      # raw, at once
    out.evidence.append(f"{name} FAILED: code={failure.get('code')} phase={failure.get('phase')} (raw diagnostic kept)")


def test_pc7_p4_flash_integrity(bench_client, qual_session, pytestconfig, loader_state, request):
    cfg = qual_session
    gate(cfg, "P4-FLASH-INTEGRITY")
    if cfg["chip"] != P4_CHIP or cfg["flash_size"] != P4_FLASH:
        pytest.skip(f"BLOCKED: this recipe reads the {P4_CHIP} with {P4_FLASH} bytes; the session declares {cfg['chip']} / {cfg['flash_size']}")
    expected = cfg["integrity"].get("expected_assets_sha256")
    if not expected:
        pytest.skip("BLOCKED: integrity.expected_assets_sha256 is not declared: the instrumented agent cannot be verified")
    storage = require_persistent_evidence(cfg, pytestconfig)           # BLOCKED unless the evidence root is on a persistent volume (or the recorded dev override)
    problem = free_space_problem(cfg["evidence_root"], 1024 * 1024)
    if problem:
        pytest.skip("BLOCKED: " + problem)
    manifest = campaign_manifest(cfg, "P4-FLASH-INTEGRITY", regions=[],
                                 steps=["Loader entry may restart the P4 (identify, then 3 chip MD5 and 2 reads with console return)"])
    case = make_campaign(bench_client, cfg, "PC-7", __file__, pytestconfig, manifest=manifest,
                         parameters={"address": ADDRESS, "size": SIZE, "expected_assets_sha256": expected, "plan": list(PLAN)})
    request.addfinalizer(lambda: sync_evidence(case.directory))       # after the report: every evidence file and directory flushed, also after a failure
    out = Outcome()
    with recorded(case, "PC-7", out, preconditions=f"{cfg['chip']} / {cfg['transport']}, agent assets {expected[:12]}",
                  limits="Read only; one range of 4 KiB; 3 chip MD5 and 2 reads; the loader entry may restart the card; digests are what the agent reported over the "
                         "same serial link; no cause is established; one run"):
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
        identity = first_command(cfg, loader_state, board.identify)
        case.write("identity.json", {k: identity.get(k) for k in ("chip", "flash_size", "flash_id")})          # never the MAC
        assert identity["chip"] == P4_CHIP and identity["flash_size"] == P4_FLASH, f"measured {identity['chip']} / {identity['flash_size']}"
        out.evidence.append(f"identify: chip={identity['chip']} flash_size={identity['flash_size']}")

        observations = []
        evidence_dir = case.directory / "board-0"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        with retained_reads(board, out) as reads:          # a server readback is deleted only after its private copy was re-read and matched
            for index, kind in enumerate(PLAN):
                name = f"{index}-{kind}"
                try:
                    if kind == "chip_md5":
                        result = board.command("flash.md5", {"address": ADDRESS, "size": SIZE, "return_console": True})
                        record = {"kind": kind, "index": index, "operation": board.last_operation["id"], "md5": result.get("md5"), "format": result.get("format"),
                                  "identify": result.get("identify")}
                        out.evidence.append(f"{name}: chip md5={record['md5']} format={record['format']}")
                    else:
                        result = board.command("flash.read", {"address": ADDRESS, "size": SIZE, "return_console": True, "stub_digest": True})
                        artifact = result["artifact"]
                        reads.held.append(dict(artifact))              # tracked at once: kept on the server unless a verified private copy exists
                        data = board.client.download(artifact)
                        durable_copy(evidence_dir, index, ADDRESS, data, artifact, board.last_operation["id"])
                        digests = result.get("stub_digests") or []
                        stub = digests[0] if len(digests) == 1 else {"state": "missing" if not digests else "unexpected-count", "count": len(digests)}
                        record = {"kind": kind, "index": index, "operation": board.last_operation["id"], "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                  "md5_client": hashlib.md5(data).hexdigest(), "stub": stub, "identify": result.get("identify")}
                        assert len(data) == SIZE and result.get("address") == ADDRESS, f"{name}: unexpected address or size"
                        out.evidence.append(f"{name}: sha256={record['sha256']} md5_client={record['md5_client']} stub={stub.get('state')} stub_md5={stub.get('md5')}")
                except OperationError as error:
                    _failure(case, name, error, out)
                    raise                                            # stops here: nothing after a failed operation, nothing replayed
                observations.append(record)
                case.write(f"{name}.json", record)

        analysis = analyse(observations)
        case.write("integrity-analysis.json", analysis)
        out.evidence.append(summary(analysis))
        for finding in analysis["findings"]:
            out.evidence.append(f"{finding['level']}: {finding['text']}")
        out.evidence.append(analysis["caution"])
        out.evidence.append("Scope: this scenario does not authorise the full P4 backup (PC-BACKUP), which keeps its own consent, evidence storage check and review")
        consoles = [e for e in board.events(cursor) if e["kind"] == "transport" and "console opened" in (e.get("detail") or "")]
        out.evidence.append(f"console returned {len(consoles)} time(s) (transport evidence only)")
        assert analysis["conclusion"] == "consistent", f"integrity not established: {summary(analysis)}"
        assert len(consoles) >= len(PLAN) + 1, f"expected the console to return after each of the {len(PLAN) + 1} operations, saw {len(consoles)}"
