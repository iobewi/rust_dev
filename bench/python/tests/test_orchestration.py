import json
from pathlib import Path
import selectors
import subprocess
import sys
import time
from unittest.mock import Mock
import hashlib
import os

import pytest
from iobewi_bench import BenchClient, BenchError, CampaignManifest, Orchestrator
from iobewi_bench.qualification import Campaign


def manifest(root):
    return {'version':1,'id':'software-simulation',
        'boards':[{'id':'board','chip':'esp32s3','flash_size':4194304,'transports':['unknown']}],
        'write_regions':[{'board':'board','address':65536,'size':8192}],
        'max_interruptions':1,'backup_policy':'Save all affected sectors and hash privately',
        'restore_policy':'Explicit consent, verify each sector', 'grant_seconds':1800,
        'operator_steps':['Revoke grant locally at closure'],
        'start_criteria':['Verified reference board'], 'close_criteria':['Restored and grant revoked'],
        'private_evidence':str(root)}


@pytest.mark.parametrize('change',[{'version':2},{'version':True},{'max_interruptions':-1},
    {'grant_seconds':0},{'write_regions':[{'board':'board','address':65537,'size':4096}]},
    {'boards':[{'id':'board','chip':'esp32s3','flash_size':4194304,'transports':['imaginary']}]},
    {'write_regions':[{'board':'board','address':65536,'size':4096}]*2}])
def test_invalid_manifests_rejected_before_hardware(tmp_path,change):
    with pytest.raises(ValueError):
        CampaignManifest({**manifest(tmp_path),**change})


def test_manifest_hash_and_erase_sector_bounds(tmp_path):
    m = CampaignManifest(manifest(tmp_path))
    assert len(m.sha256) == 64
    m.document['write_regions'].clear()  # caller cannot mutate validated constraints
    m.check_region('board',65537,5)
    with pytest.raises(ValueError):
        m.check_region('board',65535,2)
    board = Mock(device_id='board')
    board.client.device.return_value = {'flash_size':4194304}
    for name,args in [('flash.erase',{}),('flash.write',{'erase':True})]:
        with pytest.raises(ValueError):
            m.check_command(board,name,args)


def test_manual_interruption_budget_and_report_history(sim_bench,tmp_path):
    case = Campaign(sim_bench,'board',tmp_path/'case',__file__,'report',manifest=manifest(tmp_path))
    runner = Orchestrator(case)
    runner.manual_window('unplug explicitly',interruption=True)
    with pytest.raises(ValueError):
        runner.manual_window('unplanned second interruption',interruption=True)
    case.result('T1','BLOCKED',preconditions='No physical card',evidence=[],incidents=[],
                restoration='untouched',limits='software only')
    with pytest.raises(ValueError):
        case.result('T1','PASS',preconditions='',evidence=[],incidents=[],restoration='',limits='')
    assert json.loads((case.directory/'report.json').read_text())['tests'][0]['status'] == 'BLOCKED'


@pytest.fixture
def granted_sim():
    process = subprocess.Popen([sys.executable,str(Path(__file__).with_name('sim_server.py')),'--grant','1800'],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        selector = selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
        assert selector.select(10), 'RAM simulator did not start'
        selector.close()
        client = BenchClient(json.loads(process.stdout.readline())['url'],'pytest-client-test-token')
        deadline = time.monotonic()+10
        while not client.devices():
            assert time.monotonic()<deadline
            time.sleep(.01)
        client.wait_device('board',timeout=10,grant_seconds=600,recovered=True)
        yield client
    finally:
        process.terminate();process.communicate(timeout=5)


def test_explicit_backup_program_restore_via_public_api(granted_sim,tmp_path):
    case = Campaign(granted_sim,'board',tmp_path/'proofs',__file__,'roundtrip',
                    manifest=manifest(tmp_path),allow_destructive=True)
    case.start();board = case.reserve();runner = Orchestrator(case)
    backup, metadata = runner.backup(board,address=65536,size=8192,filename='reference.bin',segment_size=4096)
    assert metadata['sha256'] == hashlib.sha256(b'\xff'*8192).hexdigest()
    data = b'qualification-test'*200
    with pytest.raises(BenchError,match='consent'):
        runner.program(board,address=65536,source=data,sha256=hashlib.sha256(data).hexdigest())
    before = case.client.submissions
    with pytest.raises(ValueError,match='manifest'):
        runner.program(board,address=0,source=data,sha256=hashlib.sha256(data).hexdigest(),allow_destructive=True)
    assert case.client.submissions == before
    runner.program(board,address=65536,source=data,sha256=hashlib.sha256(data).hexdigest(),allow_destructive=True)
    assert runner.restore(board,backup,allow_destructive=True)['restored'] is True
    assert runner.compare(board,backup)['equal'] is True
    # No remote revocation endpoint: teardown correctly refuses the still-active local grant.
    with pytest.raises(ExceptionGroup) as failure:
        case.close()
    assert any('grant' in str(e) for e in failure.value.exceptions)
    assert granted_sim.device('board')['reservation'] is None


def test_manifest_guards_generic_command_before_submission(granted_sim,tmp_path):
    case = Campaign(granted_sim,'board',tmp_path/'bounded',__file__,'bounded',
                    manifest=manifest(tmp_path),allow_destructive=True)
    case.start();board = case.reserve()
    before = case.client.submissions
    with pytest.raises(ValueError,match='manifest'):
        board.command('flash.erase',{'chip':'esp32s3'},allow_destructive=True)
    assert case.client.submissions == before
    board.close()


def test_destructive_campaign_checks_program_scope_before_reserving(sim_bench,tmp_path):
    source = BenchClient(sim_bench.url,'pytest-second-test-token')
    case = Campaign(source,'board',tmp_path/'scopes',__file__,'scopes',allow_destructive=True)
    with pytest.raises(AssertionError,match='program scope'):
        case.start()
    assert case.client.submissions == 0
    assert sim_bench.device('board')['reservation'] is None


def backup_double(tmp_path):
    board = Mock(output_dir=tmp_path)
    board.read_flash.return_value = b'x' * 4096
    board.last_operation = {'id':'read-1','result':{'artifact':{'id':'artifact-1'}}}
    return board, Orchestrator(Mock(manifest=Mock()))


def test_backup_is_synced_and_rechecked_before_artifact_deletion(tmp_path,monkeypatch):
    from iobewi_bench import orchestration
    board, runner = backup_double(tmp_path)
    synced = []
    original = orchestration.os.fsync

    def sync(descriptor):
        original(descriptor)
        synced.append(os.fstat(descriptor).st_size)

    def cleanup(artifact):
        assert artifact == 'artifact-1'
        assert 4096 in synced
        assert (tmp_path/'reference.bin.partial').read_bytes() == b'x'*4096
        segment = json.loads((tmp_path/'reference.bin.partial.jsonl').read_text())
        assert segment['address'] == 65536
        assert segment['sha256'] == hashlib.sha256(b'x'*4096).hexdigest()

    monkeypatch.setattr(orchestration.os,'fsync',sync)
    board.client.delete_artifact.side_effect = cleanup
    path, metadata = runner.backup(board,address=65536,size=4096,filename='reference.bin')
    assert path.read_bytes() == b'x'*4096
    assert json.loads(path.with_suffix('.json').read_text()) == metadata
    assert not list(tmp_path.glob('*.partial*'))


@pytest.mark.parametrize('failure_call',[1,2,3])
def test_backup_disk_failure_retains_server_artifact(tmp_path,monkeypatch,failure_call):
    from iobewi_bench import orchestration
    board, runner = backup_double(tmp_path)
    calls = 0
    original = orchestration.os.fsync

    def sync(descriptor):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError('simulated disk failure')
        original(descriptor)

    monkeypatch.setattr(orchestration.os,'fsync',sync)
    with pytest.raises(OSError,match='disk failure'):
        runner.backup(board,address=65536,size=4096,filename='reference.bin')
    board.client.delete_artifact.assert_not_called()
    assert not (tmp_path/'reference.json').exists()


def test_backup_does_not_overwrite_concurrently_created_evidence(tmp_path):
    board, runner = backup_double(tmp_path)

    def read(**args):
        (tmp_path/'reference.json').write_text('historical evidence')
        return b'x'*4096

    board.read_flash.side_effect = read
    with pytest.raises(FileExistsError):
        runner.backup(board,address=65536,size=4096,filename='reference.bin')
    assert (tmp_path/'reference.json').read_text() == 'historical evidence'
    assert (tmp_path/'reference.bin.partial').read_bytes() == b'x'*4096


def test_backup_rejects_filename_colliding_with_metadata(tmp_path):
    board, runner = backup_double(tmp_path)
    with pytest.raises(ValueError):
        runner.backup(board,address=65536,size=4096,filename='reference.json')
    board.read_flash.assert_not_called()


def test_restore_refuses_reference_modified_between_segments(tmp_path):
    board, runner = backup_double(tmp_path)
    path = tmp_path/'reference.bin'
    data = b'x'*(8*1024*1024)
    path.write_bytes(data)
    path.with_suffix('.json').write_text(json.dumps({'complete':True,'address':0,
        'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}))

    def mutate_after_first_segment(*args,**kwargs):
        with path.open('r+b') as stream:
            stream.seek(4*1024*1024)
            stream.write(b'changed')

    runner.program = Mock(side_effect=mutate_after_first_segment)
    with pytest.raises(BenchError,match='changed after integrity'):
        runner.restore(board,path,allow_destructive=True)
    assert runner.program.call_count == 1
