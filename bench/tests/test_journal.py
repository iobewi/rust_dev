"""Crash boundary tests for the durable broker; no physical hardware."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.core import Broker
from protocol import Fault


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Broker(state_dir=self.tmp.name)
        self.b.agent('agent', object())
        self.g = self.b.register('agent', {'id':'board', 'chip':'esp32s3',
            'flash_size':4194304, 'capabilities':['flash.write', 'identify']}, ['board'])
        self.lease = self.b.reserve('board', 'alice', 600)
        self.b.agent_event('agent', {'type':'grant', 'device':'board', 'generation':self.g, 'seconds':1800})
        self.art = self.b.artifact(b'x'*4096, hashlib.sha256(b'x'*4096).hexdigest(), 'client:alice')
        self.op = self.b.submit('alice', ['read','control','program'], {
            'lease':self.lease['id'], 'command':'flash.write', 'allow_destructive':True,
            'args':{'chip':'esp32s3','images':[{'artifact':self.art['id'], 'sha256':self.art['sha256'], 'address':65536}]}})

    def tearDown(self):
        self.b.journal.close()
        self.tmp.cleanup()

    def restart(self):
        epoch = self.b.epoch
        self.b.journal.close()
        self.b = Broker(state_dir=self.tmp.name)
        self.assertNotEqual(epoch, self.b.epoch)
        self.assertFalse(self.b.leases[self.lease['id']]['active'])
        return self.b.operation(self.op['id'], 'alice')

    def start(self):
        self.b.offer('board')
        self.b.commit('agent', self.op['id'])

    def test_restart_before_execute_never_replays(self):
        for offered in [False, True]:
            if offered:
                # Previously cancelled operations remain cancelled on another restart.
                self.b.operations[self.op['id']]['state'] = 'offered'
                self.b.journal.save(self.b)
            op = self.restart()
            self.assertEqual(op['state'], 'cancelled')
            self.assertEqual(op['error']['code'], 'server_restart_before_execution')

    def test_restart_during_execution_pins_data_and_requires_reconciliation(self):
        self.start()
        op = self.restart()
        self.assertEqual(op['state'], 'indeterminate')
        self.assertTrue(op['reconciliation_required'])
        self.assertTrue(self.b.device('board')['quarantined'])
        self.assertEqual(self.b.view(self.b.device('board'))['privileged_seconds'], 0)
        self.assertTrue(self.b.artifact_pinned(self.art['id']))
        self.assertEqual(self.b.artifacts[self.art['id']]['data'], b'x'*4096)
        with self.assertRaises(Fault):
            self.b.finish('agent', {'id':self.op['id'], 'state':'completed'})
        self.b.agent('agent', object())
        generation = self.b.register('agent', {'id':'board','chip':'esp32s3',
            'flash_size':4194304, 'capabilities':['identify']}, ['board'])
        self.assertIsNone(self.b.offer('board'))
        self.b.agent_event('agent', {'type':'recover', 'device':'board','generation':generation})
        self.assertFalse(self.b.device('board')['quarantined'])
        self.assertEqual(self.b.operation(self.op['id'], 'alice')['state'], 'indeterminate')
        self.assertFalse(self.b.artifact_pinned(self.art['id']))

    def test_result_persisted_before_ack_survives_with_verification(self):
        self.start()
        result = {'ok':True,'verification':{'md5':'verified','sha256_readback':'not-run'},
                  'items':[{'addr':65536,'sha256':self.art['sha256'],'size':4096}],
                  'trace':'sensitive text'}
        self.b.finish('agent', {'id':self.op['id'],'state':'completed','result':result})
        op = self.restart()
        self.assertEqual(op['state'], 'completed')
        self.assertEqual(op['result']['verification']['md5'], 'verified')
        self.assertNotIn('trace', op['result'])
        self.assertEqual(len(op['result_sha256']),64)
        self.assertTrue(op['reconciliation_required'])
        self.assertTrue(self.b.device('board')['quarantined'])

    def test_settled_result_survives_without_new_uncertainty(self):
        self.start()
        self.b.finish('agent', {'id':self.op['id'],'state':'completed','result':{'ok':True}})
        self.b.agent_event('agent', {'type':'settled','device':'board','generation':self.g,'id':self.op['id']})
        op = self.restart()
        self.assertEqual(op['state'],'completed')
        self.assertTrue(op['settled'])
        self.assertFalse(self.b.device('board')['quarantined'])

    def test_journal_failure_prevents_execute_and_latches_closed(self):
        self.b.offer('board')
        original = self.b.journal.save
        self.b.journal.save = lambda _: (_ for _ in ()).throw(OSError('disk full'))
        with self.assertRaises(Fault) as error:
            self.b.commit('agent', self.op['id'])
        self.assertEqual(error.exception.code, 'journal_unavailable')
        self.b.journal.save = original
        with self.assertRaises(Fault):
            self.b.offer('board')
        op = self.restart()
        self.assertEqual(op['state'], 'cancelled')  # no execute response was emitted

    def test_second_authoritative_process_is_refused(self):
        with self.assertRaises(BlockingIOError):
            Broker(state_dir=self.tmp.name)
