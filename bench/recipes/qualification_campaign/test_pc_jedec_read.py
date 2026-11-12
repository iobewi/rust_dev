"""P4-JEDEC-READ (G1) - two independent reads of 4096 bytes at 0x2000 on the ESP32-P4, each followed by the console return, with the raw JEDEC
diagnostics of every identification kept in the evidence. READ ONLY: no write, no erase, no reset command.

Why: the first hardware run read 0x2000 once, then the second read failed in the `identify` phase (an invalid JEDEC id is suspected, never observed).
The agent now reports, for every flash command, the raw JEDEC id its identification read, the rank of that identification in the loader session and
whether the stub runs, and a failed identification raises `flash_capacity_unknown` with the same fields. This recipe keeps them.

Preconditions: the page assets digest reported by the agent equals `jedec.expected_assets_sha256` of the private session (the operator reviewed and
deployed that agent); the card is the ESP32-P4 with 32 MiB. Each read uses return_console=true, so every read opens a fresh loader session. The identity
check is NOT relaxed, cached or retried anywhere.

Criteria, written before any run:
  PASS           both reads completed with the declared size at 0x2000, equal SHA-256, the console returned after each operation, the diagnostics of every
                 identification present, no change of target, generation or epoch, reservation released;
  FAIL           an identification or a read failed definitively (the raw JEDEC id, if the agent gave one, is recorded), the two reads differ, or the agent
                 did not report the diagnostics;
  INDETERMINATE  an operation is unresolved or the proof window broke: nothing is replayed;
  BLOCKED        no consent, not the P4 / 32 MiB, no declared assets digest, the agent runs another version.
On the first FAIL or INDETERMINATE the recipe stops. The loader entry may restart the card. Hardware status: NOT executed.
"""
import hashlib
import json
from importlib.metadata import version

import pytest

from iobewi_bench.client import OperationError

from .harness import Blocked, Outcome, make_campaign, recorded
from .pc import first_command, gate, loader_state, open_loader_window      # noqa: F401  (loader_state is a fixture)
from .readonly import record_transport, retained_reads, start_or_block
from .session import build_manifest as campaign_manifest

pytestmark = pytest.mark.bench
P4_CHIP, P4_FLASH = "esp32p4", 33554432
ADDRESS, SIZE = 0x2000, 4096


def test_pc6_p4_jedec_two_reads(bench_client, qual_session, pytestconfig, loader_state):
    cfg = qual_session
    gate(cfg, "P4-JEDEC-READ")
    if cfg["chip"] != P4_CHIP or cfg["flash_size"] != P4_FLASH:
        pytest.skip(f"BLOCKED: this recipe reads the {P4_CHIP} with {P4_FLASH} bytes; the session declares {cfg['chip']} / {cfg['flash_size']}")
    expected = cfg["jedec"].get("expected_assets_sha256")
    if not expected:
        pytest.skip("BLOCKED: jedec.expected_assets_sha256 is not declared: the instrumented agent cannot be verified")
    manifest = campaign_manifest(cfg, "P4-JEDEC-READ", regions=[], steps=["Loader entry may restart the P4 (identify, then two reads with console return)"])
    case = make_campaign(bench_client, cfg, "PC-6", __file__, pytestconfig, manifest=manifest,
                         parameters={"address": ADDRESS, "size": SIZE, "expected_assets_sha256": expected})
    out = Outcome()
    with recorded(case, "PC-6", out, preconditions=f"{cfg['chip']} / {cfg['transport']}, agent assets {expected[:12]}",
                  limits="Read only; two reads of 4 KiB; the loader entry may restart the card; the JEDEC values are what the agent reported, not an independent "
                         "measurement; one run; says nothing about longer or chained reads"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        device = bench_client.device(cfg["board_id"])
        provenance = device.get("provenance") or {}
        health = bench_client.health()
        case.write("versions.json", {"server": health.get("build"), "epoch_prefix": health["epoch"][:8], "agent_sha256": provenance.get("agent_sha256"),
                                    "page_assets_sha256": provenance.get("assets_sha256"), "expected_assets_sha256": expected,
                                    "client": version("iobewi-bench"), "generation_prefix": device["generation"][:8]})
        if provenance.get("assets_sha256") != expected:
            raise Blocked(f"the agent reports page assets {str(provenance.get('assets_sha256'))[:12]}, the session expects {expected[:12]}: "
                          "not the reviewed instrumentation; no command was submitted")
        out.evidence.append(f"agent page assets {expected[:12]} as declared; server build {str((health.get('build') or {}).get('git_sha'))[:9]}")
        board = case.reserve(ttl=600)
        open_loader_window(case, board, cfg, out, loader_state)
        cursor = board.cursor()
        identity = first_command(cfg, loader_state, board.identify)
        case.write("identity.json", {k: identity.get(k) for k in ("chip", "flash_size", "flash_id")})          # never the MAC
        assert identity["chip"] == P4_CHIP and identity["flash_size"] == P4_FLASH, f"measured {identity['chip']} / {identity['flash_size']}"
        flash_id = identity.get("flash_id")
        out.evidence.append(f"identify: chip={identity['chip']} flash_size={identity['flash_size']} flash_id="
                            + (f"{flash_id:#x}" if isinstance(flash_id, int) else "not reported"))

        records = []
        with retained_reads(board, out) as reads:
            for number in (1, 2):
                try:
                    data = reads.read(address=ADDRESS, size=SIZE, return_console=True)
                except OperationError as error:
                    failure = error.operation.get("error") or {}
                    case.write(f"read-{number}-failure.json", {"operation": error.operation.get("id"), "state": error.operation.get("state"), "error": failure})
                    out.evidence.append(f"read {number} FAILED: code={failure.get('code')} phase={failure.get('phase')} jedec_id={failure.get('jedec_id')} "
                                        f"identify_index={failure.get('identify_index')} loader_stub={failure.get('loader_stub')} (raw diagnostic kept)")
                    raise
                result = board.last_operation.get("result") or {}
                info = result.get("identify")
                record = {"number": number, "operation": board.last_operation["id"], "address": ADDRESS, "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(), "identify": info}
                records.append(record)
                case.write(f"read-{number}.json", record)
                assert info and {"jedec_id", "identify_index", "loader_stub"} <= set(info), f"read {number}: the agent did not report the JEDEC diagnostics"
                assert result.get("address") == ADDRESS and len(data) == SIZE, f"read {number}: unexpected address or size"
                out.evidence.append(f"read {number}: {SIZE} bytes at {ADDRESS:#x} sha256={record['sha256']} jedec_id={info['jedec_id']:#08x} "
                                    f"identify_index={info['identify_index']} loader_stub={info['loader_stub']}")
        assert records[0]["sha256"] == records[1]["sha256"], "the two independent reads differ"
        consoles = [e for e in board.events(cursor) if e["kind"] == "transport" and "console opened" in (e.get("detail") or "")]
        out.evidence.append(f"console returned {len(consoles)} time(s) for identify and 2 reads (transport evidence only)")
        assert len(consoles) >= 3, f"expected the console to return after each of the 3 operations, saw {len(consoles)}"
        case.write("jedec-summary.json", {"identify_flash_id": identity.get("flash_id"), "reads": records})
