import base64
import hashlib
from unittest.mock import Mock
import pytest
from iobewi_bench import BenchClient, BenchError, Board, ContinuityError, PendingOperation
from iobewi_bench.client import Cursor, BenchHTTPError, OperationError


def fake_board(*, operations=None, batch=None):
    client = Mock()
    client.poll_interval = .001
    client.request_timeout = .01
    client.health.return_value = {"epoch": "epoch"}
    device = {"id": "board", "generation": "generation", "connected": True}
    client.device.return_value = device
    if operations is not None:
        client._request.side_effect = operations
    elif batch is not None:
        client._request.return_value = batch
    return Board(client, device, "epoch", "lease", False, None)


def operation(**changes):
    return {"id": "op", "state": "completed", "result": {"chip": "esp32s3"},
            "unsettled": False, "requires_settle": True, "settled": True, **changes}


def test_real_read_reservation_and_artifacts(sim_bench, tmp_path):
    with sim_bench.reserve("board", output_dir=tmp_path) as board:
        assert board.identify()["simulated"] is True
        assert board.read_flash(address=0x10000, size=4096) == b"\xff" * 4096
        assert board.read_flash(address=0x10000, size=4096) == b"\xff" * 4096
        board.renew(ttl=60)
    assert sim_bench.device("board")["reservation"] is None
    assert len(list(tmp_path.glob("*.bin"))) == 2
    assert "pytest-client-test-token" not in (tmp_path / "operations.jsonl").read_text()


def test_server_refusal_is_not_a_pending_submission(sim_bench):
    with sim_bench.reserve("board") as board:
        with pytest.raises(BenchError, match="grant_required"):
            board.command("uart.write", {"data": "AA=="})
        assert not board.uncertain
        assert board.identify()["chip"] == "esp32s3"
    assert sim_bench.device("board")["reservation"] is None


def test_concurrency_and_read_scope(sim_bench):
    with sim_bench.reserve("board"):
        with pytest.raises(BenchError, match="device_reserved"):
            sim_bench.reserve("board")
    reader = BenchClient(sim_bench.url, "pytest-reader-test-token")
    with pytest.raises(BenchError, match="403"):
        reader.reserve("board")
    assert sim_bench.device("board")["reservation"] is None


def test_waits_for_settlement_before_returning():
    board = fake_board(operations=[operation(settled=False), operation(), {"released": True}])
    assert board.identify()["chip"] == "esp32s3"
    assert board.client._request.call_count == 2
    board.close()
    assert board.client._request.call_args.args[0] == "DELETE"


def test_lost_submission_response_is_never_replayed_or_released():
    board = fake_board(operations=BenchError("connection lost"))
    with pytest.raises(BenchError):
        board.identify()
    with pytest.raises(PendingOperation):
        board.identify()
    with pytest.raises(PendingOperation):
        board.close()
    assert board.client._request.call_count == 1


def test_unsettled_timeout_retains_reservation():
    board = fake_board(operations=[operation(state="indeterminate", unsettled=True), operation(state="indeterminate", unsettled=True)])
    with pytest.raises(PendingOperation):
        board.identify()
    with pytest.raises(PendingOperation):
        board.close()
    assert all(call.args[0] != "DELETE" for call in board.client._request.call_args_list)


@pytest.mark.parametrize("field,value", [("generation", "other"), ("connected", False)])
def test_changed_device_prevents_submission(field, value):
    board = fake_board()
    board.client.device.return_value[field] = value
    with pytest.raises(ContinuityError):
        board.identify()
    board.client._request.assert_not_called()


def test_changed_epoch_prevents_submission():
    board = fake_board()
    board.client.health.return_value = {"epoch": "new"}
    with pytest.raises(ContinuityError):
        board.identify()
    board.client._request.assert_not_called()


def test_destructive_calls_need_two_explicit_opt_ins():
    board = fake_board(operations=[operation()])
    with pytest.raises(BenchError, match="opt-in"):
        board.command("flash.erase", {"chip": "esp32s3"}, allow_destructive=True)
    board.allow_destructive = True
    with pytest.raises(BenchError, match="opt-in"):
        board.command("flash.erase", {"chip": "esp32s3"})
    board.client._request.assert_not_called()


def test_artifact_digest_is_verified(monkeypatch):
    client = BenchClient("http://localhost:8080", "dummy-token")
    monkeypatch.setattr(client, "_request", lambda *a, **k: b"1234")
    with pytest.raises(BenchError, match="SHA-256"):
        client.download({"id": "dump", "size": 4, "sha256": hashlib.sha256(b"5678").hexdigest()})


def test_uart_pattern_crosses_chunks():
    batch = {"epoch": "epoch", "lost": 0, "next": 2, "events": [
        {"kind": "uart", "data": base64.b64encode(data).decode()} for data in [b"boot RE", b"ADY\r\n"]]}
    board = fake_board(batch=batch)
    assert board.wait_uart(b"READY", cursor=Cursor("epoch", "generation", 0)) == b"READY"


@pytest.mark.parametrize("batch", [
    {"epoch": "other", "lost": 0, "next": 2, "events": []},
    {"epoch": "epoch", "lost": 1, "next": 2, "events": []},
    {"epoch": "epoch", "lost": 0, "next": 2, "events": [{"kind": "disconnected"}]},
    {"epoch": "epoch", "lost": 0, "next": 2, "events": [{"kind": "uart", "source_lost": 1}]},
    {"epoch": "epoch", "lost": 0, "next": 2, "events": [{"kind": "connected", "generation": "other"}]},
])
def test_uart_assertions_refuse_discontinuity(batch):
    board = fake_board(batch=batch)
    with pytest.raises(ContinuityError):
        board.wait_uart(b"READY", cursor=Cursor("epoch", "generation", 0))


def test_uart_wait_is_bounded():
    board = fake_board(batch={"epoch": "epoch", "lost": 0, "next": 0, "events": []})
    with pytest.raises(TimeoutError):
        board.wait_uart(b"missing", cursor=Cursor("epoch", "generation", 0), timeout=.01)


def test_failed_operation_is_reported_and_can_be_cleaned_up():
    board = fake_board(operations=[operation(state="failed", error={"code": "serial_timeout"}), {"released": True}])
    with pytest.raises(OperationError, match="serial_timeout"):
        board.identify()
    board.close()
    assert board.closed


def test_expired_reservation_cleanup_is_idempotent():
    board = fake_board(operations=BenchHTTPError(409, "lease_expired"))
    board.close()
    board.close()
    assert board.client._request.call_count == 1


def test_unsettled_completed_operation_hits_a_bounded_client_deadline():
    board = fake_board(batch=operation(settled=False))
    with pytest.raises(PendingOperation):
        board.command("identify", timeout=.1, start_timeout=.1)
    assert board.pending == "op"


def test_submitted_arguments_cursors_and_matches_are_recorded_as_raw_evidence(sim_bench, tmp_path):
    import json
    with sim_bench.reserve("board", output_dir=tmp_path) as board:
        cursor = board.cursor()
        board.read_flash(address=0x10000, size=4096)
        board.identify()
    commands = [json.loads(l) for l in (tmp_path / "commands.jsonl").read_text().splitlines()]
    assert [(c["seq"], c["command"]) for c in commands] == [(0, "flash.read"), (1, "identify")]
    assert commands[0]["args"] == {"address": 0x10000, "size": 4096, "return_console": True}
    assert commands[0]["args_sha256"] == hashlib.sha256(b'{"address":65536,"return_console":true,"size":4096}').hexdigest()
    ops = [json.loads(l) for l in (tmp_path / "operations.jsonl").read_text().splitlines()]
    assert {o["submit_seq"] for o in ops if o["command"] == "flash.read"} == {0}
    assert (cursor.index, cursor.submitted) == (0, 0)
    assert json.loads((tmp_path / "cursors.jsonl").read_text())["submitted"] == 0
    assert "pytest-client-test-token" not in (tmp_path / "commands.jsonl").read_text()


def test_the_submission_is_recorded_even_when_the_response_is_lost(tmp_path):
    import json
    board = fake_board(operations=[RuntimeError("connection lost")])
    board.output_dir = tmp_path
    with pytest.raises(RuntimeError):
        board.command("reset")
    assert board.uncertain
    assert [json.loads(l)["command"] for l in (tmp_path / "commands.jsonl").read_text().splitlines()] == ["reset"]
    assert not (tmp_path / "operations.jsonl").exists()
