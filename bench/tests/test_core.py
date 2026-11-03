import base64
import hashlib
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.core import Broker
from protocol import Fault


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.b = Broker(clock=lambda: self.now, max_events=3)
        self.b.agent('agent', object())
        self.generation = self.b.register('agent', {'id': 'board', 'chip': 'esp32s3', 'flash_size': 4*1024*1024,
            'capabilities': ['identify', 'reset', 'flash.read', 'flash.write', 'flash.erase', 'uart.write']}, ['board'])
        self.lease = self.b.reserve('board', 'alice', 100)
        self.b.devices['board']['grant_until'] = 200

    def submit(self, command='identify', args=None, **opts):
        return self.b.submit('alice', ['read', 'control', 'program'], {
            'lease': self.lease['id'], 'command': command, 'args': args or {}, **opts})

    def start(self, **opts):
        op = self.submit(**opts); self.b.offer('board'); self.b.commit('agent', op['id']); return op

    def test_exclusive_and_owner(self):
        with self.assertRaises(Fault): self.b.reserve('board', 'bob', 10)
        with self.assertRaises(Fault): self.b.lease(self.lease['id'], 'bob')
        self.b.release(self.lease['id'], 'alice')
        self.assertTrue(self.b.reserve('board', 'bob', 10))

    def test_expired_queued_never_offered(self):
        op = self.submit(ttl=1); self.now += 2
        self.assertIsNone(self.b.offer('board'))
        self.assertEqual(self.b.operations[op['id']]['state'], 'expired')

    def test_offer_expired_before_ready(self):
        op = self.submit(ttl=1); self.b.offer('board'); self.now += 2
        with self.assertRaises(Fault): self.b.commit('agent', op['id'])
        self.assertEqual(self.b.operations[op['id']]['state'], 'expired')

    def test_duplicate_ready_and_result_rejected(self):
        op = self.start()
        with self.assertRaises(Fault): self.b.commit('agent', op['id'])
        self.b.finish('agent', {'id': op['id'], 'state': 'completed'})
        with self.assertRaises(Fault): self.b.finish('agent', {'id': op['id'], 'state': 'completed'})

    def test_disconnect_running_indeterminate_no_replay(self):
        op = self.start(); self.b.disconnect('agent')
        self.assertEqual(self.b.operations[op['id']]['state'], 'indeterminate')
        self.assertTrue(self.b.devices['board']['quarantined'])
        self.b.agent('agent', object())
        self.b.register('agent', {'id': 'board', 'chip': 'esp32s3', 'flash_size': 4194304, 'capabilities': ['identify']}, ['board'])
        self.assertIsNone(self.b.offer('board'))
        with self.assertRaises(Fault): self.b.finish('agent', {'id': op['id'], 'state': 'completed'})

    def test_disconnect_offered_cancels(self):
        op=self.submit(); self.b.offer('board'); self.b.disconnect('agent')
        self.assertEqual(self.b.operations[op['id']]['state'], 'cancelled')

    def test_timeout_late_success_is_evidence_only(self):
        op=self.start(timeout=1); self.now+=2; self.b.sweep()
        self.b.finish('agent', {'id':op['id'],'state':'completed','result':{'late':True}})
        result=self.b.operation(op['id'],'alice')
        self.assertEqual(result['state'],'indeterminate'); self.assertEqual(result['late_result']['state'],'completed')

    def test_cancellation_distinct_from_interruption(self):
        op=self.start(); self.assertEqual(self.b.cancel(op['id'],'alice')['type'],'cancel')
        self.assertEqual(self.b.operations[op['id']]['state'],'running')
        self.b.finish('agent',{'id':op['id'],'state':'completed'})
        self.assertTrue(self.b.operations[op['id']]['cancel_requested'])

    def test_cancel_queued_never_offered(self):
        op=self.submit(); self.b.cancel(op['id'],'alice'); self.assertIsNone(self.b.offer('board'))

    def test_lease_expiry_during_execution(self):
        op=self.start(); self.now+=101; self.b.sweep()
        self.assertEqual(self.b.operations[op['id']]['state'],'indeterminate')
        with self.assertRaises(Fault): self.b.lease(self.lease['id'],'alice')

    def test_uart_sequence_loss_and_ring_loss(self):
        for seq in [1,3,4,5]:
            self.b.agent_event('agent', {'type':'uart','device':'board','generation':self.generation,'seq':seq,'data':base64.b64encode(bytes([seq])).decode()})
        data=self.b.events('board',0)
        self.assertEqual(data['lost'],2); self.assertEqual(data['events'][0]['source_lost'],1)
        self.assertEqual(base64.b64decode(data['events'][-1]['data']),b'\x05')
        with self.assertRaises(Fault): self.b.agent_event('agent', {'type':'uart','device':'board','generation':self.generation,'seq':5,'data':''})

    def test_flash_bounds_overlap_digest_chip_and_permissions(self):
        artifact=self.b.artifact(b'x'*64,hashlib.sha256(b'x'*64).hexdigest())
        image={'artifact':artifact['id'],'sha256':artifact['sha256'],'address':0x10000}
        good={'chip':'esp32s3','images':[image]}
        for args in [dict(good,chip='esp32c3'),dict(good,images=[dict(image,address=-1)]),
                     dict(good,images=[dict(image,address=4194303)]),
                     dict(good,images=[image,dict(image,address=0x10100)]),
                     dict(good,images=[dict(image,sha256='bad')])]:
            with self.assertRaises(Fault):self.submit('flash.write',args,allow_destructive=True)
        with self.assertRaises(Fault):self.submit('flash.write',good)
        self.b.devices['board']['grant_until']=99
        with self.assertRaises(Fault):self.submit('flash.write',good,allow_destructive=True)
        self.b.devices['board']['grant_until']=200
        with self.assertRaises(Fault):self.b.submit('alice',['read','control'],{'lease':self.lease['id'],'command':'flash.write','args':good,'allow_destructive':True})
        self.assertEqual(self.submit('flash.write',good,allow_destructive=True)['state'],'queued')

    def test_privilege_revocation_between_offer_and_start(self):
        op=self.submit('uart.write',{'data':'AA=='}); self.b.offer('board'); self.b.devices['board']['grant_until']=0
        with self.assertRaises(Fault):self.b.commit('agent',op['id'])

    def test_artifact_integrity_and_capacity(self):
        with self.assertRaises(Fault):self.b.artifact(b'123','bad')
        a=self.b.artifact(bytes(range(256)),hashlib.sha256(bytes(range(256))).hexdigest())
        self.assertEqual(self.b.artifacts[a['id']]['data'],bytes(range(256)))

    def test_read_only_scope_cannot_submit(self):
        with self.assertRaises(Fault): self.b.submit('alice',['read'],{'lease':self.lease['id'],'command':'identify'})

    def test_recovery_cannot_overlap_running_operation(self):
        op=self.start(); self.b.devices['board']['quarantined']=True
        with self.assertRaises(Fault):self.b.agent_event('agent',{'type':'recover','device':'board','generation':self.generation})
        self.b.finish('agent',{'id':op['id'],'state':'indeterminate'})
        self.b.agent_event('agent',{'type':'recover','device':'board','generation':self.generation})
        self.assertFalse(self.b.devices['board']['quarantined'])

    def test_readback_possible_in_quarantine_control_refused(self):
        self.b.devices['board']['quarantined']=True
        self.assertEqual(self.submit('flash.read',{'address':0,'size':4096})['state'],'queued')
        with self.assertRaises(Fault): self.submit('reset')

    def test_strict_numbers_and_unknown_fields(self):
        for args in [{'address':True,'size':1},{'address':0,'size':1,'shell':'bad'},{'address':0,'size':float('nan')}]:
            with self.assertRaises(Fault):self.submit('flash.read',args)
        with self.assertRaises(Fault):self.submit(ttl=float('inf'))

    def test_agents_are_bound_to_devices(self):
        with self.assertRaises(Fault):self.b.register('other',{'id':'board'},['unrelated'])

    def test_artifact_retention(self):
        a=self.b.artifact(b'x',hashlib.sha256(b'x').hexdigest());self.now+=3601;self.b.sweep()
        self.assertNotIn(a['id'],self.b.artifacts)

    def test_http_allowlist_private_literal_and_permissions(self):
        from protocol import local_origin, validate_command
        self.assertEqual(local_origin('http://192.168.1.50:80/health'),'http://192.168.1.50')
        for url in ['http://127.0.0.1/','http://169.254.169.254/','http://example.com/','https://192.168.1.50/','http://user:pass@10.0.0.1/']:
            with self.assertRaises(Fault):local_origin(url)
        d={**self.b.devices['board'],'capabilities':['http.request'],'http_origins':['http://192.168.1.50']}
        validate_command('http.request',{'url':'http://192.168.1.50/health'},d,{})
        with self.assertRaises(Fault):validate_command('http.request',{'url':'http://192.168.1.51/'},d,{})

    def test_pending_control_is_cancelled_after_quarantine(self):
        op=self.submit('reset');self.b.devices['board']['quarantined']=True
        self.assertIsNone(self.b.offer('board'));self.assertEqual(self.b.operations[op['id']]['state'],'cancelled')

    def test_events_paginate_without_skipping_or_oversized_sse_lines(self):
        from collections import deque
        import json
        d=self.b.devices['board'];d['events']=deque(maxlen=100)
        for seq in range(1,11):
            self.b.agent_event('agent',{'type':'uart','device':'board','generation':self.generation,'seq':seq,'data':base64.b64encode(b'\x00'*16384).decode()})
        after=1;seen=[]
        while after<d['seq']:
            batch=self.b.events('board',after);self.assertLess(len(json.dumps(batch)),65536)
            self.assertEqual(batch['lost'],0);seen.extend(e['seq'] for e in batch['events']);after=batch['next']
        self.assertEqual(seen,list(range(2,12)))

    def test_execute_transmits_exact_server_execution_deadline(self):
        op=self.submit(ttl=30, timeout=2)
        self.b.offer('board');self.now+=.5
        execute=self.b.commit('agent',op['id'])
        self.assertEqual(execute['execution_deadline_ms'],102500)
        self.assertEqual(execute['execution_deadline_ms'],self.b.operations[op['id']]['deadline']*1000)
        self.assertEqual(execute['deadline_ms'],130000)

    def test_late_result_enforces_timeout_without_waiting_for_dispatcher_tick(self):
        op=self.start(timeout=1);self.now+=1.01
        self.b.finish('agent',{'id':op['id'],'state':'completed','result':{'verified':True}})
        result=self.b.operation(op['id'],'alice')
        self.assertEqual(result['state'],'indeterminate')
        self.assertEqual(result['late_result']['state'],'completed')
        self.assertTrue(self.b.devices['board']['quarantined'])

    def test_agent_can_report_failed_console_return_as_quarantine(self):
        self.b.agent_event('agent',{'type':'hold','device':'board','generation':self.generation})
        self.assertTrue(self.b.devices['board']['quarantined'])
        with self.assertRaises(Fault):self.submit('reset')

    def test_device_view_reports_reservation_owner_without_disclosing_reservation_id(self):
        view=self.b.view(self.b.devices['board'])
        self.assertEqual(view['reservation'],{'owner':'alice','remaining_seconds':100})
        self.assertNotIn(self.lease['id'],str(view))
        self.b.release(self.lease['id'],'alice')
        self.assertIsNone(self.b.view(self.b.devices['board'])['reservation'])
