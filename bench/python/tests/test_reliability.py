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
