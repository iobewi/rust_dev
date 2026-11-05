"""Software regressions; these tests never access physical USB devices."""
import hashlib
import json
from unittest.mock import Mock

import pytest
from iobewi_bench import BenchClient, BenchError
from iobewi_bench.qualification import Campaign
from test_client import fake_board, operation


def test_upload_binary_path_and_buffers(sim_bench, tmp_path):
    data = bytes(range(256)) * 16
    path = tmp_path / 'image.bin'
    path.write_bytes(data)
    for source in [path, data, bytearray(data), memoryview(data)]:
        artifact = sim_bench.upload(source)
        assert artifact['sha256'] == hashlib.sha256(data).hexdigest()
        assert sim_bench.download(artifact) == data


@pytest.mark.parametrize('source', [b'', bytes(16 * 1024 * 1024 + 1)])
def test_upload_invalid_size_never_sends(source):
    client = BenchClient('http://localhost:8080', 'test-token')
    client._request = Mock()
    with pytest.raises(ValueError):
        client.upload(source)
    client._request.assert_not_called()


def test_upload_bad_response_is_never_retried():
    client = BenchClient('http://localhost:8080', 'test-token')
    client._request = Mock(side_effect=[{'storage': {'available': 100}}, {'id': 'x', 'size': 3, 'sha256': 'invalid'}])
    with pytest.raises(BenchError, match='SHA-256'):
        client.upload(b'123')
    assert client._request.call_count == 2


def test_read_flash_exposes_loader_session():
    board = fake_board(operations=[operation(result={'artifact': {'id': 'x'}})])
    board.client.download.return_value = b'ab'
    assert board.read_flash(address=0, size=2, return_console=False) == b'ab'
    assert board.client._request.call_args.args[2]['args']['return_console'] is False


def test_campaign_propagates_consent_but_keeps_per_operation_guard(sim_bench, tmp_path):
    case = Campaign(sim_bench, 'board', tmp_path / 'proofs', __file__, 'destructive', allow_destructive=True)
    case.start()
    board = case.reserve()
    assert board.allow_destructive is True
    with pytest.raises(BenchError, match='per-command'):
        board.command('flash.erase', {'chip': 'esp32s3'})
    # The RAM agent has no grant: explicit consent alone grants no authority.
    with pytest.raises(BenchError, match='grant_required'):
        board.command('flash.erase', {'chip': 'esp32s3'}, allow_destructive=True)
    case.close()
    assert json.loads((case.directory / 'run.json').read_text())['allow_destructive'] is True


def test_grant_wait_is_observation_only(sim_bench, tmp_path):
    case = Campaign(sim_bench, 'board', tmp_path / 'proofs', __file__, 'wait', allow_destructive=True)
    with pytest.raises(TimeoutError):
        case.wait_grant(minimum_seconds=1, timeout=.02)
    assert case.client.submissions == 0
    assert sim_bench.device('board')['privileged_seconds'] == 0


def test_storage_inventory_and_owner_cleanup(sim_bench):
    before = sim_bench.artifacts()['storage']['used']
    artifact = sim_bench.upload(b'cleanup-test')
    assert sim_bench.artifacts()['storage']['used'] == before + 12
    other = BenchClient(sim_bench.url, 'pytest-second-test-token')
    with pytest.raises(BenchError, match='403'):
        other.delete_artifact(artifact['id'])
    sim_bench.delete_artifact(artifact['id'])
    assert sim_bench.artifacts()['storage']['used'] == before


def test_indeterminate_even_acknowledged_is_never_a_definitive_failure():
    from iobewi_bench import PendingOperation
    board = fake_board(operations=[operation(state='indeterminate', settled=True)])
    with pytest.raises(PendingOperation):
        board.identify()
    with pytest.raises(PendingOperation):
        board.identify()
    assert board.client._request.call_count == 1


def test_campaign_windows_are_explicit_and_preserve_previous_evidence(sim_bench, tmp_path):
    from iobewi_bench import ContinuityError
    case = Campaign(sim_bench, 'board', tmp_path / 'windows', __file__, 'windows')
    case.start()
    before = (case.directory / 'before.json').read_bytes()
    with pytest.raises(ContinuityError):
        case.open_window('must first close')
    case.interrupt_window('planned server restart boundary')
    case.open_window('new session after operator checks')
    assert case.window == 1
    assert (case.directory / 'before.json').read_bytes() == before
    entries = [json.loads(line) for line in (case.directory / 'windows.jsonl').read_text().splitlines()]
    assert [e['event'] for e in entries] == ['interrupted','opened']
    case.close()


def test_server_and_client_provenance_are_recorded(sim_bench, tmp_path):
    case = Campaign(sim_bench,'board',tmp_path/'provenance',__file__,'provenance')
    case.start()
    snapshot = json.loads((case.directory/'before.json').read_text())
    assert len(snapshot['server_observed']['server_sha256']) == 64
    assert snapshot['server_observed']['source'] == 'observed_local_files'
    assert snapshot['device']['provenance_source'] == 'agent_reported'
    case.close()


def test_uncertain_handle_can_only_retire_in_a_new_locally_recovered_session(tmp_path):
    from iobewi_bench import PendingOperation
    board = fake_board()
    board.output_dir = tmp_path
    board.uncertain = True
    for changes in [{}, {'generation':'new'}, {'generation':'new','quarantined':True,'recovered_at':123}]:
        board.client.device.return_value = {'id':'board','generation':'generation','connected':True,
            'quarantined':False,'reservation':None,**changes}
        with pytest.raises(PendingOperation):
            board.retire_after_recovery()
    board.client.device.return_value.update(generation='new',quarantined=False,recovered_at=123)
    board.retire_after_recovery()
    assert board.closed and board.uncertain
    assert json.loads((tmp_path/'retired.json').read_text())['outcome'] == 'not_certified'


def test_maximum_binary_artifact_is_accepted_and_cleaned(sim_bench):
    data = b'\xa5' * (16*1024*1024)
    artifact = sim_bench.upload(data)
    assert sim_bench.download(artifact) == data
    sim_bench.delete_artifact(artifact['id'])


def test_campaign_device_disconnection_breaks_the_proof(sim_bench,tmp_path,monkeypatch):
    from iobewi_bench import ContinuityError
    case = Campaign(sim_bench,'board',tmp_path/'broken-window',__file__,'broken-window')
    case.start()
    batch = {'epoch':case.epoch,'lost':0,'next':case.after+1,'latest':case.after+1,
             'events':[{'kind':'disconnected','seq':case.after+1}]}
    monkeypatch.setattr(case.client,'_request',lambda *args,**kwargs:batch)
    with pytest.raises(ContinuityError):
        case.events()
    case.interrupt_window('disconnect observed')
    assert json.loads((case.directory/'windows.jsonl').read_text().splitlines()[-1])['capture_error'] == 'ContinuityError'


def test_waiting_for_a_not_yet_registered_board_is_passive():
    client = BenchClient('http://localhost:8080','test-token',poll_interval=.001)
    client.devices = Mock(side_effect=[[],[{'id':'board','connected':True,'quarantined':False,'privileged_seconds':0}]])
    client._request = Mock()
    assert client.wait_device('board',timeout=.1,recovered=True)['connected'] is True
    client._request.assert_not_called()


def test_proof_window_can_be_explicitly_closed_while_server_is_offline(sim_bench,tmp_path,monkeypatch):
    case = Campaign(sim_bench,'board',tmp_path/'offline-window',__file__,'offline-window')
    case.start()
    monkeypatch.setattr(case.client,'_request',Mock(side_effect=BenchError('offline')))
    case.interrupt_window('server stopped intentionally')
    assert case.after is None
    record = json.loads((case.directory/'windows.jsonl').read_text().splitlines()[-1])
    assert record['capture_error'] == 'BenchError'
    assert record['event'] == 'interrupted'


def test_epoch_change_during_polling_stops_old_session_without_another_request():
    from iobewi_bench import ContinuityError
    board = fake_board(batch=operation(settled=False))
    board.client.health.side_effect = [{'epoch':'epoch'},{'epoch':'new'}]
    with pytest.raises(ContinuityError):
        board.identify()
    assert board.pending == 'op'
    assert board.client._request.call_count == 1
