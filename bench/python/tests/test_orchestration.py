import json
from pathlib import Path
import selectors
import subprocess
import sys
import time
from unittest.mock import Mock
import hashlib

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
