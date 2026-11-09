"""Software checks of the M40 recipes (issue #40). SIMULATION ONLY: two RAM simulated cards ('board', 'board2'); no real
ESP32, no Web Serial, no browser. They prove the recipe's decisions (PASS / FAIL / BLOCKED / INDETERMINATE), its session
validation and its evidence layout. They prove NOTHING about the real cards, the UI or the physical wiring."""
import json
import os
import selectors
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from iobewi_bench import BenchClient

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
from qualification_campaign import m40                                       # noqa: E402
from qualification_campaign.session import SessionError, validate_session   # noqa: E402

RECIPE = "recipes/qualification_campaign/test_m40_multiboard.py"
TOKEN = "pytest-client-test-token"
EXAMPLE = BENCH / "recipes/qualification_campaign/session.m40.example.json"


def session(**change):
    document = {"version": 1, "board_id": "board", "chip": "esp32s3", "flash_size": 4194304, "transport": "external_uart",
                "partition_table_offset": None, "evidence_root": "/private/evidence", "accept_unknown_transport": True,
                "consent": {"scenarios": ["M40-11"]}, "second_board": {"board_id": "board2"},
                "m40": {"read": {"address": 0x100000, "size": 0x40000}, "avoid": [{"address": 0x200000, "size": 0x1000}],
                        "sample_interval_s": 0.1}}
    return {**document, **change}


# --- session ------------------------------------------------------------------------------------------------------
def test_the_versioned_example_is_valid_and_grants_no_consent():
    cfg = validate_session(json.loads(EXAMPLE.read_text()))
    assert cfg["consent"]["scenarios"] == [], "a template must never authorise hardware work"
    assert cfg["m40"]["read"] == {"address": 0x300000, "size": 0x40000}
    assert cfg["second_board"] == {"board_id": "esp32-zero"} and cfg["board_id"] == "esp32-local"
    assert all(not (0x300000 < a["address"] + a["size"] and a["address"] < 0x340000) for a in cfg["m40"]["avoid"])


def test_a_session_without_m40_is_valid_and_the_scenario_would_be_blocked():
    cfg = validate_session(session(m40={}))
    assert cfg["m40"] == {}


@pytest.mark.parametrize("section", [
    {"unknown": 1}, {"avoid": []}, {"read": {"address": 0x100000}}, {"read": {"address": 0x100001, "size": 0x1000}},
    {"read": {"address": 0x100000, "size": 0x1001}}, {"read": {"address": 0x3FF000, "size": 0x2000}},
    {"read": {"address": 0, "size": 0}}, {"read": {"address": 0x100000, "size": 0x200000}},
    {"read": {"address": 0x100000, "size": 0x40000}, "avoid": [{"address": 0x110000, "size": 0x1000}]},
    {"read": {"address": 0x100000, "size": 0x40000}, "avoid": [{"address": 0x1}]},
    {"read": {"address": 0x100000, "size": 0x40000}, "avoid": "config"},
    {"read": {"address": 0x100000, "size": 0x40000}, "sample_interval_s": 0},
    {"read": {"address": 0x100000, "size": 0x40000}, "sample_interval_s": True},
    {"read": {"address": 0x100000, "size": 0x40000}, "sample_interval_s": 60}])
def test_invalid_m40_sections_are_refused(section):
    with pytest.raises(SessionError):
        validate_session(session(m40=section))


def test_the_consent_must_be_a_known_scenario():
    with pytest.raises(SessionError):
        validate_session(session(consent={"scenarios": ["M40-99"]}))
    assert validate_session(session())["consent"]["scenarios"] == ["M40-11"]


# --- pure decisions ------------------------------------------------------------------------------------------------
def card(**change):
    return {"agent": "browser-s-1", "generation": "g1", "connected": True, "quarantined": False, "reservation": None,
            "privileged_seconds": 0, "seq": 10, **change}


def result(samples=3, events=(), **change):
    """A consistent, complete observation of an untouched card (override any field of the card with keyword arguments)."""
    base = m40.observed(card())
    rows = [{"t": float(i), "wall": 1000.0 + i, **m40.observed(card(**change))} for i in range(samples)]
    events = list(events)
    latest = events[-1]["seq"] if events else base["seq"]
    pages = [{"after": base["seq"], "next": latest, "latest": latest, "lost": 0, "count": len(events), "epoch": "epoch-1"}]
    return m40.WatchResult(baseline=base, epoch="epoch-1", samples=rows, events=events, pages=pages, terminated=True,
                           final_sampled=True, drained=True)


def uart(*seqs):
    return [{"seq": n, "kind": "uart", "generation": None} for n in seqs]


def test_readiness_and_distinctness():
    assert m40.check_ready(card(), "A") == []
    assert len(m40.check_ready(card(connected=False, quarantined=True, reservation={"owner": "x"}, privileged_seconds=5, agent=None), "A")) == 5
    assert m40.check_distinct(card(), card(agent="browser-s-2", generation="g2")) == []
    assert len(m40.check_distinct(card(), card())) == 2


def test_an_untouched_card_passes_and_spontaneous_uart_is_only_counted():
    assert m40.check_independent(result()) == [] and m40.check_complete(result()) == []
    noisy = result(events=uart(11, 12), seq=12)
    assert m40.check_independent(noisy) == [] and m40.check_complete(noisy) == [] and m40.spontaneous_uart(noisy) == 2


@pytest.mark.parametrize("change", [{"generation": "g9"}, {"agent": "other"}, {"connected": False}, {"quarantined": True},
                                    {"reservation": {"owner": "claude"}}, {"privileged_seconds": 30}])
def test_any_state_change_of_the_observed_card_is_a_violation(change):
    problems = m40.check_independent(result(**change))
    assert problems and all("changed on the observed card" in p for p in problems)


@pytest.mark.parametrize("kind", ["mark", "transport", "trigger", "grant", "connected", "disconnected", "hold", "recover"])
def test_a_non_uart_event_on_the_observed_card_is_a_violation(kind):
    problems = m40.check_independent(result(events=[{"seq": 11, "kind": kind, "generation": "g1"}], seq=11))
    assert any("non-UART event kinds" in p and kind in p for p in problems)


# --- integrity of the observation (INDETERMINATE, never a pass) -------------------------------------------------------
def tamper(outcome, **change):
    for key, value in change.items():
        setattr(outcome, key, value)
    return outcome


def test_an_incomplete_observation_is_never_a_pass():
    assert m40.check_complete(result(samples=1))
    gap = result()
    gap.errors.append("TimeoutError: x")
    assert m40.check_complete(gap) and not m40.check_complete(result())


def test_an_epoch_change_on_any_page_makes_the_capture_untrustworthy():
    outcome = result(events=uart(11, 12), seq=12)
    outcome.pages = [{"after": 10, "next": 11, "latest": 12, "lost": 0, "count": 1, "epoch": "epoch-1"},
                     {"after": 11, "next": 12, "latest": 12, "lost": 0, "count": 1, "epoch": "epoch-2"}]
    assert any("epoch changed" in p for p in m40.check_sequence(outcome))
    assert any("event capture" in p for p in m40.check_complete(tamper(outcome, epoch_changed=True)))
    assert any("epoch changed during" in p for p in m40.check_complete(outcome))


def test_a_lost_page_is_detected():
    outcome = result(events=uart(11), seq=11)
    outcome.pages[0]["lost"] = 3
    assert any("lost" in p for p in m40.check_sequence(outcome))
    assert any("lost" in p for p in m40.check_complete(outcome))


@pytest.mark.parametrize("events,fragment", [(uart(11, 11, 12), "duplicate"), (uart(11, 13), "gap"), (uart(12, 11, 13), "reordered"),
                                              (uart(12, 13), "gap")])
def test_duplicates_gaps_and_reordering_are_detected(events, fragment):
    outcome = result(events=events, seq=events[-1]["seq"])
    outcome.pages[0]["count"] = len(events)
    assert any(fragment in p for p in m40.check_sequence(outcome)), m40.check_sequence(outcome)


def test_a_broken_cursor_chain_and_a_wrong_final_state_are_detected():
    outcome = result(events=uart(11, 12), seq=12)
    outcome.pages = [{"after": 10, "next": 11, "latest": 12, "lost": 0, "count": 1, "epoch": "epoch-1"},
                     {"after": 9, "next": 12, "latest": 12, "lost": 0, "count": 1, "epoch": "epoch-1"}]
    assert any("cursor chain" in p for p in m40.check_sequence(outcome))
    outcome = result(events=uart(11), seq=11)
    outcome.pages[0]["latest"] = 14
    outcome.pages[0]["next"] = 14
    assert any("final state" in p for p in m40.check_sequence(outcome))
    outcome = result()
    outcome.pages = []
    assert m40.check_sequence(outcome)


def test_no_final_sample_or_a_stuck_collector_is_not_a_pass():
    assert any("final synchronous sample" in p for p in m40.check_complete(tamper(result(), final_sampled=False)))
    assert any("did not terminate" in p for p in m40.check_complete(tamper(result(), terminated=False)))


def test_evidence_rows_are_timestamped_and_carry_no_payload_or_raw_identity(tmp_path):
    outcome = result(events=[{"seq": 11, "kind": "uart", "generation": None, "data": "SECRET"}], seq=11, reservation=None)
    outcome.samples[0]["agent"] = "browser-RAW-SESSION-ID"
    outcome.baseline["agent"] = "browser-RAW-SESSION-ID"
    path = tmp_path / "observed.jsonl"
    m40.write_evidence(path, outcome)
    text = path.read_text()
    assert "SECRET" not in text and "RAW-SESSION" not in text and (path.stat().st_mode & 0o777) == 0o600
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[0]["type"] == "baseline" and rows[-1]["type"] == "summary"
    samples = [r for r in rows if r["type"] == "sample"]
    assert len(samples) == 3 and all(r["wall"] > 0 and r["t"] >= 0 and len(r["agent"]) == 12 for r in samples)
    assert [r["type"] for r in rows if r["type"] in ("page", "event")] == ["page", "event"]


# --- compatibility between the running server and the recipes --------------------------------------------------------
LOCAL = "a" * 64


def health(**build):
    return {"protocol": 1, "build": {"git_sha": "x" * 40, "server_sha256": LOCAL, **build}}


def test_the_server_code_digest_decides_not_the_git_sha():
    assert m40.assess_server_build(health(git_sha="different-sha"), local=LOCAL)[:2] == (True, "same-server-code")
    ok, basis, reasons = m40.assess_server_build(health(server_sha256="b" * 64), local=LOCAL)
    assert (ok, basis) == (False, "different-server-code") and "m40.compatible_server_sha256" in reasons[0]
    assert m40.assess_server_build(health(server_sha256="b" * 64), local=LOCAL, accepted=["b" * 64])[:2] == (True, "operator-declared")
    assert m40.assess_server_build(health(server_sha256="b" * 64), local=LOCAL, accepted=["c" * 64])[0] is False


@pytest.mark.parametrize("document", [{}, {"protocol": 1}, {"protocol": 1, "build": {}}, {"protocol": 1, "build": {"server_sha256": "short"}},
                                      {"protocol": 2, "build": {"server_sha256": LOCAL}}, None])
def test_a_missing_digest_or_another_protocol_is_never_accepted(document):
    ok, basis, reasons = m40.assess_server_build(document, local=LOCAL, accepted=[LOCAL])
    assert ok is False and reasons


def test_the_local_digest_matches_the_one_a_real_server_publishes(sim):
    url, client = sim()
    published = client.health()["build"]["server_sha256"]
    assert m40.local_server_sha256() == published
    assert m40.assess_server_build(client.health())[:2] == (True, "same-server-code")


@pytest.mark.parametrize("compatible", [["A" * 64], ["a" * 63], [1], "x", ["a" * 64, "a" * 64], ["a" * 64] * 9])
def test_invalid_compatible_server_digests_are_refused(compatible):
    with pytest.raises(SessionError):
        validate_session(session(m40={"read": {"address": 0x100000, "size": 0x40000}, "compatible_server_sha256": compatible}))


def test_valid_compatible_server_digests_are_kept():
    cfg = validate_session(session(m40={"read": {"address": 0x100000, "size": 0x40000}, "compatible_server_sha256": ["a" * 64]}))
    assert cfg["m40"]["compatible_server_sha256"] == ["a" * 64]


def operation(**change):
    return {"device": "board", "generation": "g1", "command": "flash.read", "state": "completed",
            "result": {"artifact": {"id": "a" * 32, "size": 4096, "sha256": "x"}}, **change}


@pytest.mark.parametrize("change,fragment", [({"device": "board2"}, "device"), ({"generation": "g2"}, "generation"),
                                              ({"command": "identify"}, "command"), ({"state": "failed"}, "state"),
                                              ({"result": {}}, "no artifact"),
                                              ({"result": {"artifact": {"id": "a", "size": 1}}}, "size")])
def test_operation_target_and_artifact_are_checked(change, fragment):
    assert not m40.check_operation(operation(), device="board", generation="g1", command="flash.read", args={"address": 0, "size": 4096})
    problems = m40.check_operation(operation(**change), device="board", generation="g1", command="flash.read", args={"address": 0, "size": 4096})
    assert problems and any(fragment in p for p in problems)


def test_a_replayed_or_extra_command_is_detected():
    row = lambda seq, name, **args: {"seq": seq, "command": name, "args": args}                # noqa: E731
    ok = [row(0, "flash.read", address=1, size=2), row(1, "console")]
    expected = [("flash.read", {"address": 1, "size": 2}), ("console", None)]
    assert m40.check_commands(ok, expected) == []
    assert m40.check_commands(ok + [row(2, "flash.read", address=1, size=2)], expected)
    assert m40.check_commands([ok[0], ok[0], ok[1]], expected)
    assert m40.check_commands([row(0, "flash.read", address=9, size=2), row(1, "console")], expected)
    assert m40.check_commands([row(0, "flash.read", address=1, size=2), row(5, "console")], expected)


def test_target_continuity_events():
    assert m40.check_continuity([{"seq": 1, "kind": "uart"}, {"seq": 2, "kind": "transport"}], "g1") == []
    assert m40.check_continuity([{"seq": 3, "kind": "disconnected"}], "g1")
    assert m40.check_continuity([{"seq": 4, "kind": "connected", "generation": "g2"}], "g1")
    assert not m40.check_continuity([{"seq": 4, "kind": "connected", "generation": "g1"}], "g1")


def test_private_dump_verification(tmp_path):
    import hashlib
    data = bytes(range(256)) * 16
    path = tmp_path / "flash-001-00001000.bin"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    got, problems = m40.verify_private_dump(tmp_path, address=0x1000, size=len(data), sha256=digest)
    assert got == digest and not problems and (path.stat().st_mode & 0o777) == 0o600
    assert m40.verify_private_dump(tmp_path, address=0x1000, size=len(data), sha256="0" * 64)[1]
    assert m40.verify_private_dump(tmp_path, address=0x1000, size=len(data) + 1, sha256=digest)[1]
    assert m40.verify_private_dump(tmp_path, address=0x2000, size=1, sha256=digest)[0] is None


# --- simulated bench -----------------------------------------------------------------------------------------------
@pytest.fixture
def sim():
    started = []

    def start():
        process = subprocess.Popen([sys.executable, str(BENCH / "python/tests/sim_server.py"), "--second", "--chip", "esp32s3"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started.append(process)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10)
        url = json.loads(process.stdout.readline())["url"]
        client = BenchClient(url, TOKEN)
        deadline = time.monotonic() + 10
        while len(client.devices()) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return url, client
    yield start
    for process in started:
        process.terminate()
        process.communicate(timeout=5)


def test_the_watch_only_reads_and_flags_a_reservation_taken_on_the_observed_card(sim):
    url, client = sim()
    watch = m40.BoardWatch(url, TOKEN, "board2", interval=0.05)
    baseline = watch.start()
    time.sleep(0.3)
    board = client.reserve("board2", ttl=60)        # a command path on the observed card: exactly what the recipe must catch
    time.sleep(0.3)
    outcome = watch.stop()
    board.close()
    assert baseline["reservation"] is None and len(outcome.samples) >= 3 and not outcome.errors
    assert any("reservation changed" in p for p in m40.check_independent(outcome))


def test_the_watch_of_an_untouched_card_is_clean(sim):
    url, client = sim()
    watch = m40.BoardWatch(url, TOKEN, "board2", interval=0.05)
    watch.start()
    with client.reserve("board", ttl=60) as board:       # activity on the OTHER card only
        board.identify()
    time.sleep(0.2)
    outcome = watch.stop()
    assert m40.check_complete(outcome) == [] and m40.check_independent(outcome) == []


class Scripted:
    """A scripted bench client for the collector: deterministic pages, optional blocking and failures. Tracks concurrent readers."""

    def __init__(self, *, epoch="e1", seq=10, page=None, block_after=None, fail_after=None):
        self.epoch, self.seq, self.page = epoch, seq, page
        self.block_after, self.fail_after = block_after, fail_after
        self.device_calls, self.page_calls, self.gate = 0, 0, threading.Event()
        self.active, self.max_active, self._lock = 0, 0, threading.Lock()

    def _enter(self):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _leave(self):
        with self._lock:
            self.active -= 1

    def health(self):
        return {"epoch": self.epoch}

    def device(self, _):
        self._enter()
        try:
            self.device_calls += 1
            if self.block_after is not None and self.device_calls > self.block_after:
                self.gate.wait(10)
            if self.fail_after is not None and self.device_calls > self.fail_after:
                raise ConnectionError("down")
            return {"agent": "a", "generation": "g", "connected": True, "quarantined": False, "reservation": None,
                    "privileged_seconds": 0, "seq": self.seq}
        finally:
            self._leave()

    def _request(self, method, path, *args, **kwargs):
        self._enter()
        try:
            self.page_calls += 1
            after = int(path.rsplit("after=", 1)[1])
            if self.page:
                return self.page(self, after, self.page_calls)
            return {"epoch": self.epoch, "lost": 0, "events": [], "next": after, "latest": after}
        finally:
            self._leave()


def collect(client, *, settle=0.3, interval=0.02, join_timeout=2.0):
    watch = m40.BoardWatch("http://127.0.0.1:1", TOKEN, "board2", interval=interval, client=client, join_timeout=join_timeout)
    watch.start()
    time.sleep(settle)
    return watch, watch.stop()


def test_a_quiet_scripted_card_gives_a_complete_clean_observation():
    client = Scripted()
    _, outcome = collect(client)
    assert outcome.terminated and outcome.final_sampled and outcome.drained and not outcome.errors
    assert m40.check_complete(outcome) == [] and m40.check_independent(outcome) == []
    assert client.max_active == 1, "two readers touched the client at the same time"
    assert all(page["after"] == 10 for page in outcome.pages)


def test_an_epoch_change_on_a_later_page_is_indeterminate():
    def page(client, after, call):
        epoch = client.epoch if call < 4 else "e2"
        return {"epoch": epoch, "lost": 0, "events": [], "next": after, "latest": after}
    _, outcome = collect(Scripted(page=page))
    assert outcome.epoch_changed and any("epoch changed" in e for e in outcome.errors)
    assert any("epoch changed" in p for p in m40.check_complete(outcome))
    assert outcome.pages[-1]["epoch"] == "e2" and len([p for p in outcome.pages if p["epoch"] == "e2"]) == 1, "no event is read after the change"


def test_a_lost_page_is_reported_by_the_collector():
    def page(client, after, call):
        return {"epoch": "e1", "lost": 2 if call == 2 else 0, "events": [], "next": after, "latest": after}
    _, outcome = collect(Scripted(page=page))
    assert any("lost" in e for e in outcome.errors) and any("lost" in p for p in m40.check_complete(outcome))


def test_a_duplicated_event_is_reported_by_the_collector():
    def page(client, after, call):
        events = [{"seq": 11, "kind": "uart"}]           # the server wrongly serves the same event again after the cursor
        return {"epoch": "e1", "lost": 0, "events": events, "next": 11, "latest": 11}
    _, outcome = collect(Scripted(page=page, seq=10))
    assert any("duplicate" in p or "cursor chain" in p for p in m40.check_sequence(outcome))
    assert m40.check_complete(outcome)


def test_a_gap_between_pages_is_reported_by_the_collector():
    def page(client, after, call):
        if call == 1:
            return {"epoch": "e1", "lost": 0, "events": [{"seq": 11, "kind": "uart"}], "next": 11, "latest": 13}
        return {"epoch": "e1", "lost": 0, "events": [{"seq": 13, "kind": "uart"}], "next": 13, "latest": 13}
    _, outcome = collect(Scripted(page=page, seq=10))
    assert any("gap" in p for p in m40.check_sequence(outcome))


def test_a_stuck_collector_makes_the_proof_indeterminate_and_no_second_reader_is_started():
    client = Scripted(block_after=1)                       # baseline answers, then the collector's first read blocks forever
    watch = m40.BoardWatch("http://127.0.0.1:1", TOKEN, "board2", interval=0.02, client=client, join_timeout=0.3)
    watch.start()
    time.sleep(0.2)
    calls = client.device_calls
    outcome = watch.stop()
    assert outcome.terminated is False and not outcome.final_sampled
    assert client.device_calls == calls, "stop() read the card while the collector thread was still alive"
    assert client.max_active == 1
    assert any("did not terminate" in p for p in m40.check_complete(outcome))
    assert watch.stop() is outcome                         # idempotent
    client.gate.set()                                      # release the daemon thread


def test_a_failing_last_sample_is_reported_and_leaves_no_final_sample():
    client = Scripted(fail_after=2)                        # baseline (1) and the collector's first cycle (2) work, the final one (3) fails
    watch = m40.BoardWatch("http://127.0.0.1:1", TOKEN, "board2", interval=30, client=client, join_timeout=2.0)
    watch.start()
    time.sleep(0.3)
    outcome = watch.stop()
    assert outcome.terminated and not outcome.final_sampled
    assert any("final sample" in e for e in outcome.errors)
    assert any("final synchronous sample" in p for p in m40.check_complete(outcome))


def run_recipe(tmp_path, url, selector, session_document, *, extra=()):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({**session_document, "evidence_root": str(tmp_path / "evidence")}))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": TOKEN, "BENCH_URL": url, "BENCH_QUAL_SESSION": str(path)}
    junit = tmp_path / "junit.xml"
    run = subprocess.run([sys.executable, "-m", "pytest", f"{RECIPE}::{selector}", "--bench", "--qual-allow-dirty", "--bench-results",
                          str(tmp_path / "runs"), f"--junitxml={junit}", "-q", "-rs", "-p", "no:cacheprovider", "--tb=short", *extra],
                         cwd=BENCH, env=env, capture_output=True, text=True, timeout=180)
    reports = {}
    for report in (tmp_path / "evidence").glob("*/report.json"):
        for item in json.loads(report.read_text())["tests"]:
            reports[item["id"]] = item
    return run, reports, junit


def submitted(directory):
    rows = [json.loads(line) for path in directory.glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows if r["method"] == "POST" and r["path"] in ("/api/v1/operations", "/api/v1/leases")]


def test_m40_0_passes_on_two_ready_cards_and_sends_nothing(sim, tmp_path):
    url, client = sim()
    run, reports, junit = run_recipe(tmp_path, url, "test_m40_0_initial_state", session())
    assert run.returncode == 0 and "1 passed" in run.stdout, run.stdout[-1500:]
    assert reports["M40-0"]["status"] == "PASS"
    assert not submitted(tmp_path / "evidence"), "M40-0 must not reserve or command anything"
    state = json.loads(next((tmp_path / "evidence").glob("M40-0-*/m40-0-state.json")).read_text())
    assert set(state["cards"]) == {"board", "board2"}


def test_m40_0_is_blocked_when_the_second_card_is_missing_or_reserved(sim, tmp_path):
    url, client = sim()
    (tmp_path / "a").mkdir()
    run, reports, _ = run_recipe(tmp_path / "a", url, "test_m40_0_initial_state", session(second_board={}))
    assert "BLOCKED" in run.stdout and not reports
    (tmp_path / "b").mkdir()
    held = client.reserve("board2", ttl=120)
    try:
        run, reports, _ = run_recipe(tmp_path / "b", url, "test_m40_0_initial_state", session())
    finally:
        held.close()
    assert reports["M40-0"]["status"] == "BLOCKED" and "reserved" in " ".join(reports["M40-0"]["incidents"])


def test_m40_11a_passes_and_leaves_private_proof(sim, tmp_path):
    url, client = sim()
    before = client.device("board2")
    run, reports, junit = run_recipe(tmp_path, url, "test_m40_11a_flash_read_on_first_card_second_card_independent", session())
    assert run.returncode == 0 and "1 passed" in run.stdout, run.stdout[-2500:]
    report = reports["M40-11a"]
    assert report["status"] == "PASS", report
    folder = next((tmp_path / "evidence").glob("M40-11a-*"))
    dumps = list(folder.glob("board-*/flash-001-00100000.bin"))
    assert len(dumps) == 1 and dumps[0].stat().st_size == 0x40000 and (dumps[0].stat().st_mode & 0o777) == 0o600
    commands = [json.loads(line)["command"] for line in next(folder.glob("board-*/commands.jsonl")).read_text().splitlines()]
    assert commands == ["flash.read", "console"]
    assert len(submitted(tmp_path / "evidence")) == 3                    # one lease and the two commands, all on the first card
    after = client.device("board2")
    assert (after["agent"], after["generation"], after["reservation"]) == (before["agent"], before["generation"], None)
    assert client.device("board")["reservation"] is None
    compat = json.loads(next(folder.glob("m40-server-compat.json")).read_text())
    assert compat["compatible"] and compat["basis"] == "same-server-code" and compat["server_sha256"] == compat["local_server_sha256"]
    rows = [json.loads(line) for line in next(folder.glob("m40-observed-card.jsonl")).read_text().splitlines()]
    kinds = [r["type"] for r in rows]
    assert kinds[0] == "baseline" and kinds[-1] == "summary" and kinds.count("sample") >= 3 and "page" in kinds
    assert rows[-1]["terminated"] and rows[-1]["final_sampled"] and rows[-1]["drained"] and not rows[-1]["epoch_changed"]
    assert all(r["wall"] > 0 for r in rows if r["type"] == "sample")
    case = ET.parse(junit).getroot().find(".//testcase")
    props = {p.get("name"): p.get("value") for p in case.iter("property")}
    assert props["target"] == "board" and props["observed"] == "board2" and props["size"] == "0x40000" and len(props["dump_sha256"]) == 64
    assert not client.artifacts()["artifacts"], "the server readback must be deleted once the private copy is verified"


def test_m40_scenarios_are_blocked_when_the_running_server_code_differs_from_the_recipes(sim, tmp_path):
    """A recipe checkout whose server code differs from the RUNNING server's (and is not declared compatible) must BLOCK, sending nothing."""
    import shutil
    url, client = sim()
    copy = tmp_path / "bench"
    shutil.copytree(BENCH, copy, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".bench-runs", ".pytest_cache"))
    with (copy / "server" / "core.py").open("a") as stream:
        stream.write("\n# different server code for the compatibility test\n")
    for selector in ("test_m40_0_initial_state", "test_m40_11a_flash_read_on_first_card_second_card_independent"):
        work = tmp_path / selector
        work.mkdir()
        path = work / "session.json"
        path.write_text(json.dumps({**session(), "evidence_root": str(work / "evidence")}))
        env = {**os.environ, "BENCH_CLIENT_TOKEN": TOKEN, "BENCH_URL": url, "BENCH_QUAL_SESSION": str(path)}
        run = subprocess.run([sys.executable, "-m", "pytest", f"{RECIPE}::{selector}", "--bench", "--qual-allow-dirty", "--bench-results",
                              str(work / "runs"), "-q", "-rs", "-p", "no:cacheprovider", "--tb=short"],
                             cwd=copy, env=env, capture_output=True, text=True, timeout=120)
        assert "BLOCKED" in run.stdout and "differs from this checkout" in run.stdout, run.stdout[-1200:]
        assert not submitted(work / "evidence"), "an incompatible server must receive no reservation or command"
        report = next((work / "evidence").glob("*/m40-server-compat.json"))
        assert json.loads(report.read_text())["compatible"] is False


@pytest.mark.parametrize("change,fragment", [({"consent": {"scenarios": []}}, "M40-11"), ({"m40": {}}, "m40"),
                                              ({"second_board": {}}, "second_board")])
def test_m40_11a_is_blocked_before_any_command(sim, tmp_path, change, fragment):
    url, client = sim()
    run, reports, _ = run_recipe(tmp_path, url, "test_m40_11a_flash_read_on_first_card_second_card_independent", session(**change))
    assert "BLOCKED" in run.stdout and fragment in run.stdout and not reports
    assert not submitted(tmp_path / "evidence"), "a blocked recipe must not reserve or command anything"
    assert client.device("board")["reservation"] is None


def test_m40_11a_fails_when_the_second_card_is_touched_during_the_run(sim, tmp_path):
    """The observed card gets a reservation of ANOTHER client while the read runs: the recipe cannot PASS."""
    url, client = sim()
    stop, held = threading.Event(), []

    def intruder():
        other = BenchClient(url, "pytest-second-test-token")
        while not stop.is_set():
            if client.device("board")["reservation"] is not None:
                held.append(other.reserve("board2", ttl=60))
                return
            time.sleep(0.005)
    thread = threading.Thread(target=intruder)
    thread.start()
    try:
        run, reports, _ = run_recipe(tmp_path, url, "test_m40_11a_flash_read_on_first_card_second_card_independent", session())
    finally:
        stop.set()
        thread.join(5)
        for board in held:
            board.close()
    assert held, "the simulated intruder never got to reserve the observed card"
    assert reports["M40-11a"]["status"] == "FAIL" and "reservation changed" in " ".join(reports["M40-11a"]["incidents"])
