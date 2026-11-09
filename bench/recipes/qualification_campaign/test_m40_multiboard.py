"""M40 - multi-card qualification of IOBEWI Device Lab (issue #40) through the public API. NO write, NO erase, NO grant.

The browser keeps the physical role (USB ownership, supervision, operator consent and gestures); these recipes only
drive and observe through the REST API with the official client, never through the UI. Two cards are involved: the
FIRST (`board_id`, the target) and the SECOND (`second_board.board_id`, only OBSERVED - this module never reserves it,
never commands it and never opens a lease on it).

M40-0    initial state: both cards connected, unquarantined, unreserved, ungranted, with distinct agents and generations.
         No hardware effect. PASS / BLOCKED.
M40-11a  one `flash.read` of the configured range (m40.read) on the first card, then the console returns:
  PASS           the operation targets the first card and the generation captured BEFORE the submission; it completes once;
                 its artifact belongs to that operation (size, SHA-256) and the PRIVATE copy re-read from disk matches it;
                 exactly [flash.read, console] were submitted (no replay); the first card keeps its generation and gets no
                 disconnect / new-generation event; its reservation is released afterwards;
                 the second card keeps agent, generation, connection, quarantine, reservation and grant for the WHOLE
                 run (sampled by GET only) and receives no event other than spontaneous UART (counted, never attributed
                 to an operation);
  FAIL           any of the above is violated, a command lands on the wrong card, or a second operation exists;
  BLOCKED        no M40-11 consent / no m40 or second_board section / a card not ready / a declaration that does not match;
  INDETERMINATE  an unresolved operation or a lost proof window (epoch / generation / UART continuity, sampling gaps on the
                 second card). Nothing is replayed and a server artifact whose private copy is not verified is KEPT.
Replay (same parameters, same criteria; the token is injected into the process environment, never on the command line):
  cp recipes/qualification_campaign/session.m40.example.json <private>/session.m40.json   # then set evidence_root, consent M40-11
  python -m pytest recipes/qualification_campaign/test_m40_multiboard.py --bench --bench-url <url> --bench-device <first card> \
      --qual-session <private>/session.m40.json --bench-results <private>/runs --junitxml=<private>/junit-m40.xml -rA -p no:cacheprovider
`run.json` records the Git SHA, the tree state, the executed support files and the session file's SHA-256; the JUnit properties carry
the parameters and the dump's size and SHA-256 (never its content). `--qual-allow-dirty` is for simulations only.
Loader entry (flash.read) and the return to the console restart the first card: that is the declared M40-11 consent. The
selection of another card in the UI during the operation is NOT an API property: it is qualified by the Playwright
simulation (M40-11b) and, on hardware, by a separate operator gesture. Hardware status: NOT executed.
"""
import os

import pytest

from iobewi_bench.client import BenchError, ContinuityError

from . import m40
from .harness import Blocked, Outcome, make_campaign, recorded, source_state
from .readonly import console_on_exit, record_transport, require_consent, require_target, retained_reads, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench
TARGET_CHIP = "esp32s3"
LIMITS = ("API view only: the page, the Web Serial objects, the physical port and the MAC are not observable here; "
          "agent ids and generations prove registrations, not wiring; loader entry and console return restart the first card; "
          "the second card is only sampled (a sample proves its state at that instant); one run; says nothing about writes, "
          "more than two cards or the UI selection")


def _second_card_id(cfg):
    other = cfg["second_board"].get("board_id")
    if not other:
        pytest.skip("BLOCKED: no second_board.board_id in the session (the second card must be named, never presumed)")
    return other


def _second_card(client, other):
    try:
        return client.device(other)
    except BenchError as error:
        raise Blocked(f"the second card {other!r} is unknown to the server (not connected under that logical id)") from error


def test_m40_0_initial_state(bench_client, qual_session, pytestconfig, record_property):
    cfg = qual_session
    require_target(cfg, TARGET_CHIP)
    other = _second_card_id(cfg)
    manifest = build_manifest(cfg, "M40-0", regions=[], steps=["No operator gesture; read-only state capture of two cards"])
    case, out = make_campaign(bench_client, cfg, "M40-0", __file__, pytestconfig, manifest=manifest), Outcome()
    record_property("devices", f"{cfg['board_id']},{other}")
    with recorded(case, "M40-0", out, preconditions=f"{cfg['chip']} x2 ({cfg['board_id']}, {other})", limits=LIMITS):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        first, second = case.client.device(cfg["board_id"]), _second_card(case.client, other)
        problems = m40.check_ready(first, cfg["board_id"]) + m40.check_ready(second, other) + m40.check_distinct(first, second)
        if problems:
            raise Blocked("; ".join(problems))
        health, recipe = case.client.health(), source_state()
        state = {"epoch": health["epoch"], "server": health.get("build"), "recipe_git_sha": recipe["git_sha"],
                 "recipe_tree_clean": recipe["clean"],
                 "cards": {name: {k: card.get(k) for k in ("id", "agent", "generation", "chip", "flash_size", "provenance", "seq")}
                           for name, card in ((cfg["board_id"], first), (other, second))}}
        case.write("m40-0-state.json", state)
        server_sha = (health.get("build") or {}).get("git_sha")
        out.evidence.append(f"both cards connected, unquarantined, unreserved, ungranted; agents {first['agent'][-8:]} / {second['agent'][-8:]}; "
                            f"generations {first['generation'][:8]} / {second['generation'][:8]} (distinct)")
        out.evidence.append(f"server SHA {str(server_sha)[:12]} vs recipe SHA {recipe['git_sha'][:12]}: "
                            + ("equal" if server_sha == recipe["git_sha"] else "DIFFERENT (reported only; check before any hardware run)"))
        out.evidence.append("shared browser-session prefix of the two agents: "
                            + str(first["agent"].rsplit("-", 1)[0] == second["agent"].rsplit("-", 1)[0]) + " (reported only)")


def test_m40_11a_flash_read_on_first_card_second_card_independent(bench_client, qual_session, pytestconfig, record_property):
    cfg = qual_session
    require_target(cfg, TARGET_CHIP)
    require_consent(cfg, "M40-11")
    if not cfg["m40"]:
        pytest.skip("BLOCKED: no m40 section (m40.read range) in the private session; none is presumed")
    other, spec = _second_card_id(cfg), cfg["m40"]["read"]
    address, size = spec["address"], spec["size"]
    manifest = build_manifest(cfg, "M40-11a", regions=[], steps=["No operator gesture; the first card restarts on loader entry and console return"])
    case, out = make_campaign(bench_client, cfg, "M40-11a", __file__, pytestconfig, manifest=manifest), Outcome()
    for key, value in (("target", cfg["board_id"]), ("observed", other), ("address", hex(address)), ("size", hex(size))):
        record_property(key, value)
    token = os.environ["BENCH_CLIENT_TOKEN"]
    with recorded(case, "M40-11a", out, preconditions=f"{cfg['chip']} x2 ({cfg['board_id']} target, {other} observed); read {address:#x}+{size:#x}",
                  limits=LIMITS):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        second = _second_card(case.client, other)
        problems = m40.check_ready(second, other) + m40.check_distinct(case.client.device(cfg["board_id"]), second)
        if problems:
            raise Blocked("; ".join(problems))
        watch = m40.BoardWatch(case.client.url, token, other, interval=cfg["m40"]["sample_interval_s"])
        case.write("m40-baseline-observed-card.json", watch.start())
        result = None
        try:
            board = case.reserve(ttl=600)
            generation = board.generation
            assert generation == case.generation, "the generation captured at start differs from the reservation's"
            with console_on_exit(board, out), retained_reads(board, out) as reads:
                data = reads.read(address=address, size=size, timeout=cfg["operation_timeout_s"])
                operation = dict(board.last_operation or {})
        finally:
            result = watch.stop()
            case.write("m40-observed-card.json", {"baseline": result.baseline, "samples": len(result.samples), "events": result.events,
                                                    "errors": result.errors, "spontaneous_uart": m40.spontaneous_uart(result)})
        args = {"address": address, "size": size}
        # --- first card: the operation, its artifact, the private dump -------------------------------------------------------
        problems = m40.check_operation(operation, device=cfg["board_id"], generation=generation, command="flash.read", args=args)
        assert not problems, "; ".join(problems)
        artifact = operation["result"]["artifact"]
        assert len(data) == size, "short read"
        digest, problems = m40.verify_private_dump(board.output_dir, address=address, size=size, sha256=artifact["sha256"])
        assert not problems, "; ".join(problems)
        record_property("dump_sha256", digest)
        record_property("dump_size", size)
        # --- no replay, release, continuity ------------------------------------------------------------------------------
        problems = m40.check_commands(m40.read_commands(board.output_dir), [("flash.read", args), ("console", None)])
        assert not problems, "; ".join(problems)
        assert case.client.submissions == 2, f"{case.client.submissions} operations submitted by this client, expected 2 (flash.read, console)"
        board.close()
        after = case.client.device(cfg["board_id"])
        assert after["reservation"] is None, "the reservation of the first card is still held after release"
        assert after["generation"] == generation, "the first card changed generation during the run"
        events = m40.drain_events(case.client, cfg["board_id"], case.after, epoch=case.epoch)
        problems = m40.check_continuity(events, generation)
        if problems:
            raise ContinuityError("continuity of the target card interrupted: " + "; ".join(problems))
        kinds = sorted({event["kind"] for event in events})
        case.write("m40-target-events.json", [{k: e[k] for k in ("seq", "kind", "generation")} for e in events])
        # --- second card: independence -------------------------------------------------------------------------------------
        incomplete = m40.check_complete(result)
        if incomplete:
            raise ContinuityError("observation of the second card incomplete: " + "; ".join(incomplete))
        problems = m40.check_independent(result)
        assert not problems, "; ".join(problems)
        out.evidence.append(f"flash.read {address:#x}+{size:#x} on {cfg['board_id']} generation {generation[:8]}: completed once, "
                            f"artifact {artifact['id'][:8]} size {size}; private copy re-read, SHA-256 {digest}")
        out.evidence.append(f"submitted exactly [flash.read, console]; reservation released; generation unchanged; event kinds on the target: {kinds}")
        out.evidence.append(f"{other}: {len(result.samples)} GET samples every {result.interval}s, agent/generation/connection/quarantine/"
                            f"reservation/grant unchanged, no event but {m40.spontaneous_uart(result)} spontaneous UART (never attributed to an operation)")
