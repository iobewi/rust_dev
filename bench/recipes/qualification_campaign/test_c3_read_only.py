"""C3-R - identity, repeatable reads and partition table. NO flash write, NO erase, NO explicit reset.

Loader entry (identify / flash.read / partitions) may restart the card as an inherent side effect; that is
declared by the C3R-READ consent. The deliberate reset is a different scenario (test_c3_reset.py).
Chip, flash size, transport, table candidates and read ranges all come from the private session: nothing about
the C3 is presumed. Hardware status: NOT executed (software-reviewed only).
"""
import pytest

from .harness import Blocked, Outcome, make_campaign, recorded
from .readonly import (check_rows, console_on_exit, decode_table, gate, identify_and_confirm_console, probe_candidates,
                       record_transport, retained_reads, sha256, start_or_block)
from .session import build_manifest, table_candidates

pytestmark = pytest.mark.bench
LIMITS = ("Reads enter the loader and may restart the card; the console-return proof is about the TRANSPORT only (console "
          "opened), not about the application having restarted correctly; transport is a declaration plus an agent inference, "
          "not a measurement; one run; says nothing about firmware validity or boot policy")


def _case(bench_client, cfg, test_id, pytestconfig):
    manifest = build_manifest(cfg, "C3R", regions=[], steps=["No operator gesture; the card may restart on loader entry"])
    return make_campaign(bench_client, cfg, test_id, __file__, pytestconfig, manifest=manifest)


def test_c3r0_identity(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    gate(cfg, "C3R-READ")
    case, out = _case(bench_client, cfg, "C3R-0", pytestconfig), Outcome()
    with recorded(case, "C3R-0", out, limits=LIMITS, preconditions=f"{cfg['chip']} declared {cfg['transport']}"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=300)
        identity = identify_and_confirm_console(board, out)
        case.write("identity.json", {k: identity.get(k) for k in ("chip", "flash_size", "flash_id")})   # never the MAC
        assert identity["chip"] == cfg["chip"], f"measured {identity['chip']}, declared {cfg['chip']}"
        assert identity["flash_size"] == cfg["flash_size"], f"measured {identity['flash_size']}, declared {cfg['flash_size']}"
        out.evidence.append(f"measured chip={identity['chip']} flash_size={identity['flash_size']} (equal to the declaration)")


def test_c3r1_repeatable_reads(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    gate(cfg, "C3R-READ")
    if not cfg["readonly"]["reads"]:
        pytest.skip("BLOCKED: no readonly.reads range configured (choose immutable regions; NVS-like regions legitimately differ)")
    case, out = _case(bench_client, cfg, "C3R-1", pytestconfig), Outcome()
    with recorded(case, "C3R-1", out, limits=LIMITS, preconditions=f"{cfg['chip']} declared {cfg['transport']}"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=300)
        with console_on_exit(board, out), retained_reads(board, out) as reads:   # console back + server readbacks released
            for item in cfg["readonly"]["reads"]:
                first = reads.read(address=item["address"], size=item["size"])
                second = reads.read(address=item["address"], size=item["size"])
                assert len(first) == len(second) == item["size"], "short read"
                assert first == second, f"two reads of {item['address']:#x}+{item['size']:#x} differ (dynamic region?)"
                out.evidence.append(f"{item['address']:#x}+{item['size']:#x}: two reads equal, sha256={sha256(first)}")


def test_c3r2_partition_table(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    gate(cfg, "C3R-READ")
    candidates = table_candidates(cfg)
    if not candidates:
        pytest.skip("BLOCKED: no partition table candidate (readonly.table_candidates or partition_table_offset); none is presumed")
    case, out = _case(bench_client, cfg, "C3R-2", pytestconfig), Outcome()
    with recorded(case, "C3R-2", out, limits=LIMITS, preconditions=f"{cfg['chip']} declared {cfg['transport']}"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=300)
        with console_on_exit(board, out), retained_reads(board, out) as reads:
            valid, probes = probe_candidates(board, candidates)
            case.write("table-probes.json", probes)
            if len(valid) != 1:
                raise Blocked(f"{len(valid)} valid table(s) among {[hex(a) for a in candidates]}; give the firmware's own table address")
            address, rows = valid[0]
            raw = reads.read(address=address, size=4096)
            assert decode_table(raw) == [(r["label"], r["address"], r["size"]) for r in rows], "raw table differs from the API view"
            check_rows(rows, cfg["flash_size"])
            out.evidence.append(f"one valid table at {address:#x}: " + ", ".join(f"{r['label']}@{r['address']:#x}+{r['size']:#x}" for r in rows))
