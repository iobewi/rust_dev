"""P4-STUB-VARIANT (G1) - comparative, READ ONLY: the same short series of measurements on 0x2000 / 4096 bytes of the ESP32-P4, first with the OFFICIAL stub, then with the EXPERIMENTAL
variant `p4-no-clock-boost` (esp-flasher-stub v1.2.2 without the DCDC / 200 MHz transition on UART; bench/experiments/p4-stub-no-clock-boost). PROPOSAL: not executed on hardware.

Why: the stub's reads are unstable (PC-7, PC-8) while the ROM's MD5 is stable (PC-9). The variant keeps the same read path and ECO7 sequence and removes one factor. The variant is selected only
by this recipe, by name and with the SHA-256 pinned by the server and the agent; the official stub stays the default everywhere else.

Series (twice: official, then variant): chip MD5, read (+ the digest the stub sends), chip MD5, each in its OWN loader session (return_console=true). Six operations. The variant series runs at
115200 baud (the agent's choice for the variant), the official series at the agent's default; the MD5 of the ROM for the range (session `stub_variant.reference_rom_md5`, taken from a ROM run)
is the ground truth when given. The identity of the stub that ran, as the agent reports it (variant, SHA-256 verified, upload seen on the wire), is part of the evidence and is checked.

Preconditions: consent `P4-STUB-VARIANT` written by the operator, assets digest of the reviewed agent (`stub_variant.expected_assets_sha256`), `stub_variant.variant` and
`stub_variant.expected_sha256`, `flash.md5` announced, P4 / 32 MiB, persistent evidence. Variant commands need the per-command opt-in (`allow_stub_variant`), the programming scope and
the matching SHA-256 on the server. No write, no erase, no retry, one run.

Criteria, written before any run:
  PASS           both series complete, identities as expected, the variant series is consistent (all MD5 and received bytes equal, stub digests valid) and equals the ROM MD5 when one is given. The
                 official series is reported (SV-A when it is not stable) and not required to be stable or unstable;
  FAIL           an operation failed, an identity is not the expected one, a digest is missing, or the variant series is not consistent / not equal to the ROM MD5 (rows SV-C, SV-E are hypotheses);
  INDETERMINATE  an operation is unresolved or the proof window broke: nothing is replayed;
  BLOCKED        no consent, not the P4 / 32 MiB, no declared variant / SHA-256 / assets digest, another agent version, no `flash.md5`, evidence not persistent.
The first failing operation stops the run (console return once). Hardware status: NOT executed. A PASS does not authorise PC-BACKUP or the production use of the variant.
"""
import hashlib
from importlib.metadata import version

import pytest

from iobewi_bench.client import OperationError

from .backup import free_space_problem
from .evidence import record_storage, require_persistent_evidence
from .harness import Blocked, Outcome, make_campaign, recorded
from .integrity import PLAN_SESSION
from .pc import first_command, gate, loader_state, open_loader_window      # noqa: F401  (loader_state is a fixture)
from .readonly import _console_after_early_exit, record_transport, retained_reads, start_or_block
from .session import build_manifest as campaign_manifest
from .stub_variant import compare, summary
from .test_pc_integrity import ADDRESS, P4_CHIP, P4_FLASH, SIZE, _failure, durable_copy, sync_evidence

pytestmark = pytest.mark.bench
SCENARIO = "P4-STUB-VARIANT"


def test_pc10_p4_stub_variant_comparison(bench_client, qual_session, pytestconfig, loader_state, request):
    cfg = qual_session
    gate(cfg, SCENARIO)
    if cfg["chip"] != P4_CHIP or cfg["flash_size"] != P4_FLASH:
        pytest.skip(f"BLOCKED: this recipe measures the {P4_CHIP} with {P4_FLASH} bytes; the session declares {cfg['chip']} / {cfg['flash_size']}")
    section = cfg["stub_variant"]
    missing = [k for k in ("variant", "expected_sha256", "expected_assets_sha256") if not section.get(k)]
    if missing:
        pytest.skip(f"BLOCKED: stub_variant.{', stub_variant.'.join(missing)} not declared: the experimental stub and the reviewed agent cannot be verified")
    variant, expected_sha, expected_assets, reference = section["variant"], section["expected_sha256"], section["expected_assets_sha256"], section.get("reference_rom_md5")
    storage = require_persistent_evidence(cfg, pytestconfig)
    problem = free_space_problem(cfg["evidence_root"], 1024 * 1024)
    if problem:
        pytest.skip("BLOCKED: " + problem)
    manifest = campaign_manifest(cfg, SCENARIO, regions=[], steps=[f"Loader entry may restart the P4 (6 read-only operations, 3 with the official stub then 3 with the experimental stub {variant}, one session each)"])
    case = make_campaign(bench_client, cfg, "PC-10", __file__, pytestconfig, manifest=manifest,
                         parameters={"address": ADDRESS, "size": SIZE, "variant": variant, "expected_sha256": expected_sha, "expected_assets_sha256": expected_assets,
                                     "reference_rom_md5": reference, "series": ["official", variant], "plan": list(PLAN_SESSION)})
    request.addfinalizer(lambda: sync_evidence(case.directory))
    out = Outcome()
    with recorded(case, "PC-10", out, preconditions=f"{cfg['chip']} / {cfg['transport']}, agent assets {expected_assets[:12]}, variant {variant} {expected_sha[:12]}",
                  limits="Read only; one range of 4 KiB; 3 operations per series, one loader session each; the loader entry may restart the card; the official series is not at the variant's baud "
                         "rate; digests are what the agent reported over the same serial link; no cause is established; one run"):
        start_or_block(case)
        out.evidence.append("evidence storage: " + record_storage(case, storage))
        record_transport(case, bench_client, cfg, out)
        device = bench_client.device(cfg["board_id"])
        provenance = device.get("provenance") or {}
        health = bench_client.health()
        case.write("versions.json", {"server": health.get("build"), "epoch_prefix": health["epoch"][:8], "agent_sha256": provenance.get("agent_sha256"),
                                    "page_assets_sha256": provenance.get("assets_sha256"), "expected_assets_sha256": expected_assets, "variant": variant, "expected_sha256": expected_sha,
                                    "client": version("iobewi-bench"), "generation_prefix": device["generation"][:8]})
        if provenance.get("assets_sha256") != expected_assets:
            raise Blocked(f"the agent reports page assets {str(provenance.get('assets_sha256'))[:12]}, the session expects {expected_assets[:12]}: not the reviewed agent; no command was submitted")
        if "flash.md5" not in (device.get("capabilities") or []):
            raise Blocked("the card does not announce the flash.md5 capability; no command was submitted")
        out.evidence.append(f"agent page assets {expected_assets[:12]} as declared; flash.md5 announced; variant {variant} pinned {expected_sha[:12]}")
        board = case.reserve(ttl=900)
        open_loader_window(case, board, cfg, out, loader_state)
        evidence_dir = case.directory / "board-0"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        series = {"official": [], variant: []}
        number = 0
        try:
            with retained_reads(board, out) as reads:
                for label in ("official", variant):
                    extra = {} if label == "official" else {"stub_variant": variant, "stub_sha256": expected_sha}
                    for index, kind in enumerate(PLAN_SESSION):
                        name = f"{label}-{index}-{kind}"
                        try:
                            if kind == "chip_md5":
                                call = lambda: board.command("flash.md5", {"address": ADDRESS, "size": SIZE, "return_console": True, **extra}, allow_stub_variant=bool(extra))      # noqa: E731
                                result = first_command(cfg, loader_state, call) if number == 0 else call()
                                record = {"kind": kind, "index": index, "operation": board.last_operation["id"], "md5": result.get("md5"), "format": result.get("format"),
                                          "ram_load_ops": result.get("ram_load_ops"), "stub_identity": result.get("stub_identity"), "identify": result.get("identify")}
                                out.evidence.append(f"{name}: md5={record['md5']} stub={(record['stub_identity'] or {}).get('variant')} ram_load_ops={record['ram_load_ops']}")
                            else:
                                result = board.command("flash.read", {"address": ADDRESS, "size": SIZE, "return_console": True, "stub_digest": True, **extra}, allow_stub_variant=bool(extra))
                                artifact = result["artifact"]
                                reads.held.append(dict(artifact))
                                data = board.client.download(artifact)
                                durable_copy(evidence_dir, number, ADDRESS, data, artifact, board.last_operation["id"])
                                digests = result.get("stub_digests") or []
                                stub = digests[0] if len(digests) == 1 else {"state": "missing" if not digests else "unexpected-count", "count": len(digests)}
                                record = {"kind": kind, "index": index, "operation": board.last_operation["id"], "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                          "md5_client": hashlib.md5(data).hexdigest(), "stub": stub, "stub_identity": result.get("stub_identity"), "identify": result.get("identify")}
                                assert len(data) == SIZE and result.get("address") == ADDRESS, f"{name}: unexpected address or size"
                                out.evidence.append(f"{name}: sha256={record['sha256']} md5_client={record['md5_client']} stub={stub.get('state')} stub_md5={stub.get('md5')} "
                                                    f"stub={(record['stub_identity'] or {}).get('variant')}")
                        except OperationError as error:
                            _failure(case, name, error, out)                    # raw diagnostics, JEDEC id and rank included, kept as they are
                            raise
                        number += 1
                        series[label].append(record)
                        case.write(f"{name}.json", record)
        except BaseException as error:
            _console_after_early_exit(board, out, error)
            raise
        result = compare(series["official"], series[variant], variant_name=variant, expected_sha256=expected_sha, reference_md5=reference)
        case.write("stub-variant-comparison.json", result)
        out.evidence.append(summary(result))
        for finding in result["findings"]:
            out.evidence.append(f"{finding['level']}: {finding['text']}")
        out.evidence.append(result["caution"])
        out.evidence.append("Scope: this scenario does not authorise PC-BACKUP, the production use of the variant, or any write or erase")
        ok = result["series"]["variant"]["conclusion"] == "consistent" and result["conclusion"] in {"variant-stable", "reference-not-reproduced"} and result["variant_equals_reference"] is not False
        assert ok, f"the variant series is not established as stable and correct: {summary(result)}"
