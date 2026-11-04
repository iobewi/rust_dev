import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from iobewi_bench import BenchError, ContinuityError, OperationError
from iobewi_bench.client import BenchHTTPError
from iobewi_bench.qualification import (
    Campaign, compare_reference, expect_disconnected_read, expect_http_refusal, expect_stale_board,
)


def test_long_read_cannot_pass_with_different_contents(tmp_path):
    reference = tmp_path / "reference.bin"
    reference.write_bytes(b"reference")
    digest = hashlib.sha256(reference.read_bytes()).hexdigest()
    with pytest.raises(AssertionError, match="differs"):
        compare_reference(b"XXXXXXXXX", reference, sha256=digest, size=9)
    with pytest.raises(AssertionError, match="digest"):
        compare_reference(b"reference", reference, sha256="0" * 64, size=9)
    with pytest.raises(AssertionError, match="too short"):
        compare_reference(b"reference", reference, sha256=digest, size=10)
    assert compare_reference(b"ref", reference, sha256=digest, size=3)["equal"] is True


@pytest.mark.parametrize("error", [RuntimeError("bug"), BenchError("wrong response"),
    BenchHTTPError(403, "forbidden"), BenchHTTPError(409, "device_reserved")])
def test_expiration_does_not_accept_unrelated_errors(error):
    with pytest.raises((RuntimeError, AssertionError)):
        expect_http_refusal(Mock(side_effect=error), status=409, code="lease_expired")


def test_expiration_requires_the_expected_http_error():
    expect_http_refusal(Mock(side_effect=BenchHTTPError(409, "lease_expired")),
                        status=409, code="lease_expired")
    with pytest.raises(AssertionError, match="succeeded"):
        expect_http_refusal(lambda: None, status=409, code="lease_expired")


def failed_read(**error_changes):
    return {"id": "interrupted", "state": "failed", "result": None, "error": {
        "code": "serial_disconnected", "phase": "flash_read", "expected_bytes": 4096,
        "received_bytes": 512, **error_changes}}


@pytest.mark.parametrize("failure", [RuntimeError("bug"), ContinuityError("not a serial error"),
    OperationError(failed_read(code="serial_timeout")),
    OperationError(failed_read(received_bytes=4096))])
def test_disconnection_does_not_accept_arbitrary_exceptions(failure, tmp_path):
    board = Mock(output_dir=tmp_path)
    board.read_flash.side_effect = failure
    with pytest.raises((RuntimeError, AssertionError)):
        expect_disconnected_read(board, size=4096)


def test_interrupted_read_requires_no_exported_partial_dump(tmp_path):
    board = Mock(output_dir=tmp_path)
    board.read_flash.side_effect = OperationError(failed_read())
    assert expect_disconnected_read(board, size=4096) == "interrupted"
    (tmp_path / "flash-001.bin").write_bytes(b"partial")
    with pytest.raises(AssertionError, match="Partial dump"):
        expect_disconnected_read(board, size=4096)


def test_stale_guard_checks_post_count_even_with_correct_exception():
    client = Mock(submissions=0)
    board = Mock()
    board.identify.side_effect = ContinuityError("changed generation")
    expect_stale_board(board, client)
    def bad_implementation():
        client.submissions += 1
        raise ContinuityError("detected too late")
    board.identify.side_effect = bad_implementation
    with pytest.raises(AssertionError, match="submitted"):
        expect_stale_board(board, client)


def test_campaign_records_all_clients_and_sanitized_proofs(sim_bench, tmp_path):
    case = Campaign(sim_bench, "board", tmp_path / "proofs", __file__, "test-case")
    case.start()
    a = case.reserve()
    a.identify()
    a.close()
    second = case.independent_client("pytest-second-test-token")
    b = case.reserve(client=second)
    b.read_flash(address=0x10000, size=16)
    case.close()
    assert json.loads((case.directory / "after.json").read_text())["device"]["reservation"] is None
    reservations = (case.directory / "reservations.jsonl").read_text().splitlines()
    assert len(reservations) == 2
    requests = [json.loads(line) for line in (case.directory / "requests.jsonl").read_text().splitlines()]
    assert sum(r["method"] == "POST" and r["path"] == "/api/v1/operations" for r in requests) == 2
    assert len(list(case.directory.glob("board-*/*.bin"))) == 1
    for path in case.directory.glob("*.json*"):
        assert "pytest-client-test-token" not in path.read_text()
        assert '"data"' not in path.read_text()  # Raw UART is never copied.


def test_cleanup_attempts_every_board_and_preserves_all_failures(sim_bench, tmp_path):
    case = Campaign(sim_bench, "board", tmp_path / "proofs", __file__, "cleanup")
    case.start()
    first, second = Mock(), Mock()
    first.close.side_effect = RuntimeError("first failure")
    second.close.side_effect = ValueError("second failure")
    case.boards = [first, second]
    with pytest.raises(ExceptionGroup) as errors:
        case.close()
    first.close.assert_called_once()
    second.close.assert_called_once()
    assert len(errors.value.exceptions) == 2
    assert json.loads((case.directory / "cleanup.json").read_text())["errors"] == ["ValueError", "RuntimeError"]


@pytest.mark.parametrize("batch", [
    {"epoch": "epoch", "lost": 1, "next": 4, "latest": 4, "events": []},
    {"epoch": "changed", "lost": 0, "next": 4, "latest": 4, "events": []},
    {"epoch": "epoch", "lost": 0, "next": 4, "latest": 4,
     "events": [{"kind": "uart", "source_lost": 1, "data": "secret"}]},
])
def test_incomplete_event_proof_is_never_accepted(sim_bench, tmp_path, monkeypatch, batch):
    case = Campaign(sim_bench, "board", tmp_path / "proofs", __file__, "loss")
    case.epoch, case.after = "epoch", 0
    monkeypatch.setattr(case.client, "_request", lambda *a, **kw: batch)
    with pytest.raises(AssertionError):
        case.events()
    assert "secret" not in (case.directory / "events.jsonl").read_text()


def test_versioned_recipe_and_evidence_run_in_pytest(pytester, monkeypatch, sim_bench):
    monkeypatch.setenv("BENCH_CLIENT_TOKEN", "pytest-client-test-token")
    source = Path(__file__).resolve().parents[2] / "recipes/qualification_v2"
    pytester.makeconftest((source / "conftest.py").read_text())
    pytester.makepyfile(test_v2=(source / "test_read_only.py").read_text())
    result = pytester.runpytest_subprocess("test_v2.py::test_m1_repeatable_read", "--bench",
        "--bench-url", sim_bench.url, "--bench-device", "board", "--bench-results", str(pytester.path / "proofs"), "-q")
    result.assert_outcomes(passed=1)
    assert sim_bench.device("board")["reservation"] is None
    records = list((pytester.path / "proofs").rglob("assertions.jsonl"))
    assert len(records) == 1
    phases = [json.loads(line) for line in records[0].read_text().splitlines()]
    assert [p["phase"] for p in phases] == ["setup", "call", "teardown"]
    assert all(p["outcome"] == "passed" for p in phases)


def test_deliberate_failure_stays_a_failure_with_successful_cleanup(pytester, monkeypatch, sim_bench):
    monkeypatch.setenv("BENCH_CLIENT_TOKEN", "pytest-client-test-token")
    source = Path(__file__).resolve().parents[2] / "recipes/qualification_v2"
    pytester.makeconftest((source / "conftest.py").read_text())
    pytester.makepyfile(test_v2=(source / "test_read_only.py").read_text())
    result = pytester.runpytest_subprocess("test_v2.py::test_m4_deliberate_failure", "--bench",
        "--bench-deliberate-failure", "--bench-url", sim_bench.url, "--bench-device", "board", "-q")
    result.assert_outcomes(failed=1)
    assert sim_bench.device("board")["reservation"] is None


def test_m5_recipes_record_both_identities_and_exact_expiration(pytester, monkeypatch, sim_bench):
    monkeypatch.setenv("BENCH_CLIENT_TOKEN", "pytest-client-test-token")
    monkeypatch.setenv("BENCH_CLIENT_TOKEN_B", "pytest-second-test-token")
    source = Path(__file__).resolve().parents[2] / "recipes/qualification_v2"
    pytester.makeconftest((source / "conftest.py").read_text())
    pytester.makepyfile(test_v2=(source / "test_read_only.py").read_text())
    result = pytester.runpytest_subprocess("-k", "m5", "--bench", "--bench-url", sim_bench.url,
        "--bench-device", "board", "--bench-results", str(pytester.path / "proofs"), "-q")
    result.assert_outcomes(passed=3)
    assert len(list((pytester.path / "proofs").rglob("run.json"))) == 3
    assert sim_bench.device("board")["reservation"] is None
