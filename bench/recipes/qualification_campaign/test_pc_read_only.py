"""PC-READ (G1) - ESP32-P4 / ESP32-C6: identity, repeatable reads and partition table. NO flash write, NO erase, NO explicit reset.

Generic qualification of the bench on these two chips, the counterpart of the C3R recipes (which stay untouched and pinned to the ESP32-C3).
Loader entry (identify / flash.read / partitions) may restart the card as an inherent side effect: declared by the PC-READ consent. On a
card whose loader is entered by hand (C6 behind the H7 CP2102) the recipe waits for the ROM's declared download banner first (pc.py).
Chip, flash size, transport, loader entry, table candidates and read ranges all come from the private session. Hardware status: NOT executed.
"""
import pytest

from .harness import Blocked, Outcome, make_campaign, recorded
from .pc import first_command, gate, loader_state, open_loader_window      # noqa: F401  (loader_state is a fixture)
from .readonly import (check_rows, console_on_exit, decode_table, identify_and_confirm_console, probe_candidates, record_transport,
                       retained_reads, sha256, start_or_block)
from .session import build_manifest, table_candidates

pytestmark = pytest.mark.bench
LIMITS = ("Reads enter the loader and may restart the card; the console-return proof is about the TRANSPORT only (console opened), not "
          "about the application having restarted correctly; transport and loader entry are declarations plus an agent inference, not "
          "measurements; one run; says nothing about firmware validity, boot policy or any write")


def _case(bench_client, cfg, test_id, pytestconfig):
    manifest = build_manifest(cfg, "PC", regions=[], steps=["Manual loader entry gesture when loader.entry is 'manual'"])
    return make_campaign(bench_client, cfg, test_id, __file__, pytestconfig, manifest=manifest)


def test_pc0_identity(bench_client, qual_session, pytestconfig, loader_state):
    cfg = qual_session
    gate(cfg, "PC-READ")
    case, out = _case(bench_client, cfg, "PC-0", pytestconfig), Outcome()
    with recorded(case, "PC-0", out, limits=LIMITS, preconditions=f"{cfg['chip']} declared {cfg['transport']}, loader {cfg['loader']['entry']}"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=600)
        open_loader_window(case, board, cfg, out, loader_state)
        identity = first_command(cfg, loader_state, lambda: identify_and_confirm_console(board, out))
        case.write("identity.json", {k: identity.get(k) for k in ("chip", "flash_size", "flash_id")})   # never the MAC
        assert identity["chip"] == cfg["chip"], f"measured {identity['chip']}, declared {cfg['chip']}"
        assert identity["flash_size"] == cfg["flash_size"], f"measured {identity['flash_size']}, declared {cfg['flash_size']}"
        out.evidence.append(f"measured chip={identity['chip']} flash_size={identity['flash_size']} (equal to the declaration)")


def test_pc1_repeatable_reads(bench_client, qual_session, pytestconfig, loader_state):
    cfg = qual_session
    gate(cfg, "PC-READ")
    if not cfg["readonly"]["reads"]:
        pytest.skip("BLOCKED: no readonly.reads range configured (choose immutable regions; NVS-like regions legitimately differ)")
    case, out = _case(bench_client, cfg, "PC-1", pytestconfig), Outcome()
    with recorded(case, "PC-1", out, limits=LIMITS, preconditions=f"{cfg['chip']} declared {cfg['transport']}, loader {cfg['loader']['entry']}"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=600)
        open_loader_window(case, board, cfg, out, loader_state)
        with console_on_exit(board, out), retained_reads(board, out) as reads:
            for index, item in enumerate(cfg["readonly"]["reads"]):
                read = lambda: reads.read(address=item["address"], size=item["size"])        # noqa: E731
                first = first_command(cfg, loader_state, read) if index == 0 else read()
                second = reads.read(address=item["address"], size=item["size"])
                assert len(first) == len(second) == item["size"], "short read"
                assert first == second, f"two reads of {item['address']:#x}+{item['size']:#x} differ (dynamic region?)"
                out.evidence.append(f"{item['address']:#x}+{item['size']:#x}: two reads equal, sha256={sha256(first)}")


def test_pc2_partition_table(bench_client, qual_session, pytestconfig, loader_state):
    cfg = qual_session
    gate(cfg, "PC-READ")
    candidates = table_candidates(cfg)
    if not candidates:
        pytest.skip("BLOCKED: no partition table candidate (readonly.table_candidates or partition_table_offset); none is presumed")
    case, out = _case(bench_client, cfg, "PC-2", pytestconfig), Outcome()
    with recorded(case, "PC-2", out, limits=LIMITS, preconditions=f"{cfg['chip']} declared {cfg['transport']}, loader {cfg['loader']['entry']}"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        board = case.reserve(ttl=600)
        open_loader_window(case, board, cfg, out, loader_state)
        with console_on_exit(board, out), retained_reads(board, out) as reads:
            valid, probes = first_command(cfg, loader_state, lambda: probe_candidates(board, candidates))
            case.write("table-probes.json", probes)
            if len(valid) != 1:
                raise Blocked(f"{len(valid)} valid table(s) among {[hex(a) for a in candidates]}; give the firmware's own table address")
            address, rows = valid[0]
            raw = reads.read(address=address, size=4096)
            assert decode_table(raw) == [(r["label"], r["address"], r["size"]) for r in rows], "raw table differs from the API view"
            check_rows(rows, cfg["flash_size"])
            out.evidence.append(f"one valid table at {address:#x}: " + ", ".join(f"{r['label']}@{r['address']:#x}+{r['size']:#x}" for r in rows))
