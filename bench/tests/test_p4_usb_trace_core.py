"""Protocol and broker tests of the experimental ESP32-P4 USB OTG diagnostic.

Pure software: the broker is driven directly, no browser and no board. These tests prove the server
side of the authorization model; they say nothing about hardware behaviour."""
import base64
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.core import Broker
from protocol import Fault, P4_TRACE, p4_trace_consent, strict_equal

CMD = P4_TRACE


def stub_json(**over):
    stub = {'entry': 0x4ff50010, 'text': base64.b64encode(bytes(100)).decode(), 'text_start': 0x4ff50000,
            'data': base64.b64encode(bytes(40)).decode(), 'data_start': 0x4ff60040, 'bss_start': 0x4ff60000}
    stub.update(over)
    return json.dumps(stub).encode()


class P4TraceTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.b = Broker(clock=lambda: self.now, max_events=50, p4_stub_allowlist=[hashlib.sha256(stub_json()).hexdigest()])
        self.b.agent('agent', object())
        self.generation = self.register('p4', 'esp32p4', 'native_usb_other')
        self.b.agent('agent2', object())
        self.register('c6', 'esp32c6', 'usb_serial_jtag', agent='agent2')
        self.lease = self.b.reserve('p4', 'alice', 600)
        self.b.devices['p4']['grant_until'] = 10000
        self.stub = stub_json()
        self.artifact = self.b.artifact(self.stub, hashlib.sha256(self.stub).hexdigest(), 'alice')

    def register(self, device, chip, transport, agent='agent'):
        return self.b.register(agent, {'id': device, 'chip': chip, 'flash_size': 32 * 1024 * 1024,
            'capabilities': ['identify', 'flash.read', 'partitions', 'mark', 'flash.write', CMD] if chip == 'esp32p4' else
                            ['identify', 'flash.read', 'partitions', 'mark'],
            'provenance': {'transport': transport, 'transport_basis': 'inferred_from_webserial_vid_pid'}}, [device])

    def args(self, **over):
        sha = self.artifact['sha256']
        args = {'chip': 'esp32p4', 'stub_artifact': self.artifact['id'], 'stub_sha256': sha, 'address': 0x8000,
                'size': 1024 * 1024, 'packet_size': 4096, 'cpu_hz': 200_000_000, 'consent': p4_trace_consent('p4', sha)}
        args.update(over)
        return args

    def submit(self, args=None, scopes=('read', 'control', 'program'), allow=True, lease=None, command=CMD):
        return self.b.submit('alice', list(scopes), {'lease': (lease or self.lease)['id'], 'command': command,
                             'args': self.args() if args is None else args, 'allow_destructive': allow})

    def refused(self, code, **kw):
        with self.assertRaises(Fault) as context:
            self.submit(**kw)
        self.assertEqual(context.exception.code, code)
        self.assertEqual([o for o in self.b.operations.values() if o['command'] == CMD], [])  # nothing queued

    def start(self):
        op = self.submit(); self.b.offer('p4'); self.b.commit('agent', op['id']); return op

    # ---------------------------------------------------------------- validation of the parameters
    def test_the_nominal_request_is_accepted_and_pins_the_stub_artifact(self):
        op = self.submit()
        self.assertEqual(op['state'], 'queued')
        self.assertIn(self.artifact['id'], self.b.operations[op['id']]['artifact_ids'])

    def test_every_fixed_parameter_is_enforced_exactly(self):
        for key, bad in (('address', 0x9000), ('address', 0), ('size', 4096), ('size', 1024 * 1024 + 1), ('packet_size', 2048),
                         ('cpu_hz', 100_000_000), ('cpu_hz', 200_000_000.0), ('address', True), ('size', '1048576')):
            self.refused('diagnostic_parameter_not_allowed', args=self.args(**{key: bad}))

    def test_unknown_or_missing_fields_are_refused(self):
        self.refused('invalid_request', args={**self.args(), 'reader': 'reference'})
        args = self.args(); del args['cpu_hz']
        self.refused('diagnostic_parameter_not_allowed', args=args)

    def test_an_incompatible_mcu_or_transport_is_refused(self):
        self.b.devices['p4']['chip'] = 'esp32s3'
        self.refused('chip_mismatch')
        self.b.devices['p4']['chip'] = 'esp32p4'
        self.b.devices['p4']['provenance']['transport'] = 'external_uart'
        self.refused('diagnostic_transport_unsupported')
        self.b.devices['p4']['provenance']['transport'] = 'usb_serial_jtag'
        self.refused('diagnostic_transport_unsupported')

    def test_a_board_that_does_not_advertise_the_capability_refuses_the_command(self):
        self.b.devices['p4']['capabilities'].remove(CMD)
        self.refused('unsupported')

    def test_a_wrong_chip_argument_is_refused(self):
        self.refused('chip_mismatch', args=self.args(chip='esp32c6'))

    # ---------------------------------------------------------------- the stub artifact
    def test_a_missing_artifact_or_a_digest_mismatch_is_refused(self):
        self.refused('artifact_missing', args=self.args(stub_artifact='nope'))
        self.refused('digest_mismatch', args=self.args(stub_sha256='0' * 64,
                     consent=p4_trace_consent('p4', '0' * 64)))

    def test_a_malformed_sha_256_is_refused(self):
        for bad in ('ABC', self.artifact['sha256'].upper(), self.artifact['sha256'][:63], None, 12):
            self.refused('diagnostic_stub_sha256_invalid', args=self.args(stub_sha256=bad))

    def test_non_conforming_stub_artifacts_are_refused(self):
        cases = {'not json': b'{nope', 'wrong keys': json.dumps({'entry': 1}).encode(),
                 'text outside iram': stub_json(text_start=0x4ff50000 + 0x4500 - 50),
                 'data outside dram': stub_json(data_start=0x4ff60000 + 0x18000 - 8),
                 'bss outside dram': stub_json(bss_start=0x40000000),
                 'entry outside text': stub_json(entry=0x4ff50000 + 100), 'bad base64': stub_json(text='***'),
                 'float address': stub_json(entry=1.5), 'bool address': stub_json(text_start=True),
                 'empty text': stub_json(text='')}
        for name, data in cases.items():
            artifact = self.b.artifact(data, hashlib.sha256(data).hexdigest(), 'alice')
            sha = artifact['sha256']
            self.b.p4_stub_allowlist |= {sha}   # qualified by hash, yet still structurally validated (defense in depth)
            with self.subTest(name):
                self.refused('diagnostic_stub_invalid', args=self.args(stub_artifact=artifact['id'], stub_sha256=sha,
                             consent=p4_trace_consent('p4', sha)))

    def test_an_oversized_stub_is_refused(self):
        data = stub_json(text=base64.b64encode(bytes(70000)).decode())
        artifact = self.b.artifact(data, hashlib.sha256(data).hexdigest(), 'alice')
        self.b.p4_stub_allowlist |= {artifact['sha256']}
        self.refused('diagnostic_stub_invalid', args=self.args(stub_artifact=artifact['id'], stub_sha256=artifact['sha256'],
                     consent=p4_trace_consent('p4', artifact['sha256'])))

    # ---------------------------------------------------------------- consent and scopes
    def test_without_the_dedicated_consent_nothing_runs(self):
        args = self.args(); del args['consent']
        self.refused('diagnostic_consent_invalid', args=args)
        self.refused('diagnostic_consent_invalid', args=self.args(consent={}))

    def test_a_consent_bound_to_another_board_or_another_sha_is_refused(self):
        self.refused('diagnostic_consent_invalid', args=self.args(consent=p4_trace_consent('c6', self.artifact['sha256'])))
        other = hashlib.sha256(b'other').hexdigest()
        self.refused('diagnostic_consent_invalid', args=self.args(consent=p4_trace_consent('p4', other)))

    def test_every_clause_of_the_consent_is_required_and_types_are_strict(self):
        good = p4_trace_consent('p4', self.artifact['sha256'])
        for key in good:
            missing = copy.deepcopy(good); del missing[key]
            self.refused('diagnostic_consent_invalid', args=self.args(consent=missing))
            extra = {**good, 'extra': True}
            self.refused('diagnostic_consent_invalid', args=self.args(consent=extra))
        for key in ('ram_code_execution', 'session_effects_acknowledged', 'rom_reset_may_be_required'):
            for bad in (False, 1, 'true', None):
                self.refused('diagnostic_consent_invalid', args=self.args(consent={**good, key: bad}))
        self.refused('diagnostic_consent_invalid', args=self.args(consent={**good, 'size': 1048576.0}))

    def test_strict_equal_never_confuses_types(self):
        self.assertFalse(strict_equal({'a': 1}, {'a': True}))
        self.assertFalse(strict_equal({'a': 1}, {'a': 1.0}))
        self.assertTrue(strict_equal({'a': [1]}, {'a': [1]}))

    def test_a_read_only_or_control_only_client_cannot_start_it(self):
        for scopes, allow, code in ((('read', 'control'), True, 'program_permission_required'),
                                    (('read', 'control', 'program'), False, 'destructive_consent_required'),
                                    (('read', 'control', 'program'), None, 'destructive_consent_required')):
            self.refused(code, scopes=scopes, allow=allow)
        with self.assertRaises(Fault) as context:
            self.submit(scopes=('read',))
        self.assertEqual(context.exception.code, 'forbidden')

    def test_the_local_grant_is_required_unless_the_operator_page_itself_submits(self):
        self.b.devices['p4']['grant_until'] = 0
        self.refused('grant_required')
        op = self.b.submit('alice', ['read', 'control', 'program'], {'lease': self.lease['id'], 'command': CMD,
                           'args': self.args(), 'allow_destructive': True}, local_agent='agent')
        self.assertTrue(self.b.operations[op['id']]['local_control'])

    def test_a_standard_read_grant_flow_does_not_extend_to_the_diagnostic(self):
        read = self.b.submit('alice', ['read', 'control'], {'lease': self.lease['id'], 'command': 'flash.read',
                             'args': {'address': 0x8000, 'size': 4096}})
        self.assertEqual(read['state'], 'queued')
        self.b.cancel(read['id'], 'alice')
        self.refused('program_permission_required', scopes=('read', 'control'))

    # ---------------------------------------------------------------- exclusivity and isolation
    def test_a_concurrent_operation_on_the_same_board_is_refused_in_both_directions(self):
        queued = self.b.submit('alice', ['read', 'control'], {'lease': self.lease['id'], 'command': 'identify', 'args': {}})
        self.refused('device_busy')
        self.b.cancel(queued['id'], 'alice')
        op = self.submit()
        for command, args in (('identify', {}), ('flash.read', {'address': 0x8000, 'size': 4096}), ('mark', {'label': 'x'})):
            with self.assertRaises(Fault) as context:
                self.b.submit('alice', ['read', 'control'], {'lease': self.lease['id'], 'command': command, 'args': args})
            self.assertEqual(context.exception.code, 'diagnostic_in_progress')
        self.assertEqual(self.b.operations[op['id']]['state'], 'queued')

    def test_a_second_diagnostic_is_refused_while_one_is_pending(self):
        self.submit()
        with self.assertRaises(Fault) as context:
            self.submit()
        self.assertEqual(context.exception.code, 'diagnostic_in_progress')

    def test_another_board_keeps_working_normally_while_the_p4_runs_the_diagnostic(self):
        self.start()
        other = self.b.reserve('c6', 'bob', 600)
        read = self.b.submit('bob', ['read', 'control'], {'lease': other['id'], 'command': 'flash.read',
                             'args': {'address': 0x8000, 'size': 4096}})
        self.assertEqual(read['state'], 'queued')
        offered = self.b.offer('c6')
        self.assertEqual(offered['command'], 'flash.read')
        self.assertFalse(self.b.devices['c6']['quarantined'])
        self.assertFalse(self.b.devices['c6'].get('diagnostic_resident'))

    def test_the_diagnostic_is_refused_on_a_board_that_is_not_the_leased_p4(self):
        other = self.b.reserve('c6', 'bob', 600)
        with self.assertRaises(Fault) as context:
            self.b.submit('bob', ['read', 'control', 'program'], {'lease': other['id'], 'command': CMD,
                          'args': self.args(), 'allow_destructive': True})
        self.assertIn(context.exception.code, {'unsupported', 'diagnostic_chip_unsupported', 'chip_mismatch'})

    # ---------------------------------------------------------------- state after the run
    def test_starting_marks_the_board_resident_durably_and_finishing_quarantines_it(self):
        op = self.start()
        self.assertTrue(self.b.devices['p4']['diagnostic_resident'])
        self.b.finish('agent', {'id': op['id'], 'state': 'completed', 'result': {'ok': True}})
        self.assertTrue(self.b.devices['p4']['quarantined'])   # even without a hold flag from the agent
        self.assertTrue(self.b.devices['p4']['diagnostic_resident'])

    def test_while_resident_only_mark_is_accepted_not_even_a_standard_read(self):
        op = self.start()
        self.b.finish('agent', {'id': op['id'], 'state': 'completed', 'result': {}})
        self.b.agent_event('agent', {'type': 'settled', 'device': 'p4', 'generation': self.generation, 'id': op['id']})
        for command, args in (('identify', {}), ('partitions', {}), ('flash.read', {'address': 0x8000, 'size': 4096})):
            with self.assertRaises(Fault) as context:
                self.b.submit('alice', ['read', 'control'], {'lease': self.lease['id'], 'command': command, 'args': args})
            self.assertEqual(context.exception.code, 'device_quarantined', command)
        mark = self.b.submit('alice', ['read', 'control'], {'lease': self.lease['id'], 'command': 'mark', 'args': {'label': 'x'}})
        self.assertEqual(mark['state'], 'queued')

    def test_a_queued_standard_read_is_cancelled_not_run_through_a_resident_stub(self):
        read = self.b.submit('alice', ['read', 'control'], {'lease': self.lease['id'], 'command': 'flash.read',
                             'args': {'address': 0x8000, 'size': 4096}})
        d = self.b.devices['p4']
        d['quarantined'], d['diagnostic_resident'] = True, True     # the diagnostic ran after the read was queued
        self.assertIsNone(self.b.offer('p4'))
        self.assertEqual(self.b.operations[read['id']]['state'], 'cancelled')
        self.assertEqual(self.b.operations[read['id']]['error'], {'code': 'device_quarantined'})

    def test_a_stub_outside_the_qualified_list_is_refused_and_an_empty_list_disables_the_feature(self):
        other = stub_json(entry=0x4ff50020)
        art = self.b.artifact(other, hashlib.sha256(other).hexdigest(), 'alice')
        self.refused('diagnostic_stub_not_qualified', args=self.args(stub_artifact=art['id'], stub_sha256=art['sha256'],
                     consent=p4_trace_consent('p4', art['sha256'])))
        self.b.p4_stub_allowlist = frozenset()
        self.refused('diagnostic_stub_not_qualified')

    def test_recovery_of_a_non_diagnostic_quarantine_is_unchanged(self):
        self.b.devices['p4']['quarantined'] = True
        self.b.agent_event('agent', {'type': 'recover', 'device': 'p4', 'generation': self.generation})
        self.assertFalse(self.b.devices['p4']['quarantined'])

    def test_the_operator_local_recovery_is_the_only_way_back(self):
        op = self.start()
        self.b.finish('agent', {'id': op['id'], 'state': 'completed', 'result': {}})
        self.b.agent_event('agent', {'type': 'settled', 'device': 'p4', 'generation': self.generation, 'id': op['id']})
        with self.assertRaises(Fault) as context:   # the operator's word alone is not proof of a ROM return
            self.b.agent_event('agent', {'type': 'recover', 'device': 'p4', 'generation': self.generation})
        self.assertEqual(context.exception.code, 'rom_proof_required')
        self.assertTrue(self.b.devices['p4']['quarantined'] and self.b.devices['p4']['diagnostic_resident'])
        with self.assertRaises(Fault):
            self.b.agent_event('agent', {'type': 'recover', 'device': 'p4', 'generation': self.generation, 'rom_verified': 'true'})
        self.b.agent_event('agent', {'type': 'recover', 'device': 'p4', 'generation': self.generation, 'rom_verified': True})
        d = self.b.devices['p4']
        self.assertFalse(d['quarantined']); self.assertFalse(d['diagnostic_resident'])
        read = self.b.submit('alice', ['read', 'control'], {'lease': self.lease['id'], 'command': 'flash.read',
                             'args': {'address': 0x8000, 'size': 4096}})
        self.assertEqual(read['state'], 'queued')

    # ---------------------------------------------------------------- interruptions
    def test_a_structured_failure_report_is_stored_verbatim_and_an_uncertain_state_stays_quarantined(self):
        op = self.start()
        error = {'code': 'diagnostic_stub_reported_running', 'phase': 'diagnostic_stub_load', 'category': 'refused_before_load',
                 'ram_load': 'not_started', 'origin_code': 'diagnostic_stub_reported_running', 'origin_error_type': 'IdlabDiagnosticError',
                 'sync_values': [0] * 8, 'sync_zero_count': 8}
        self.b.finish('agent', {'id': op['id'], 'state': 'failed', 'error': error})
        self.assertEqual(self.b.operations[op['id']]['error'], error)       # the exact cause reaches the operation view
        self.assertEqual(self.b.operations[op['id']]['state'], 'failed')
        self.assertTrue(self.b.devices['p4']['quarantined'] and self.b.devices['p4']['diagnostic_resident'])   # a stub is reported: stay safe

    def refusal(self, code, **over):
        return {'code': code, 'phase': 'diagnostic_stub_load', 'category': 'refused_before_load', 'ram_load': 'not_started',
                'origin_code': code, 'origin_error_type': 'IdlabDiagnosticError', **over}

    def test_every_uncertain_admission_outcome_keeps_the_quarantine_and_the_resident_flag(self):
        for code in ('diagnostic_stub_reported_running', 'diagnostic_session_state_indeterminate', 'diagnostic_admission_sync_failed',
                     'diagnostic_stub_requires_a_rom_session', 'diagnostic_stub_ram_load_failed', 'something_unknown'):
            with self.subTest(code=code):
                self.setUp()
                op = self.start()
                self.b.finish('agent', {'id': op['id'], 'state': 'failed', 'error': self.refusal(code)})
                self.assertTrue(self.b.devices['p4']['quarantined'] and self.b.devices['p4']['diagnostic_resident'])

    def test_a_refusal_proving_nothing_reached_the_chip_restores_the_flags_held_before_the_command(self):
        for code in ('diagnostic_consent_invalid', 'diagnostic_stub_wrong_chip', 'diagnostic_stub_unsupported_chip_revision', 'artifact_integrity_failed'):
            with self.subTest(code=code):
                self.setUp()
                op = self.start()
                self.assertTrue(self.b.devices['p4']['diagnostic_resident'])          # durable mark while it runs
                self.b.finish('agent', {'id': op['id'], 'state': 'failed', 'error': self.refusal(code)})
                d = self.b.devices['p4']
                self.assertFalse(d['quarantined']); self.assertFalse(d['diagnostic_resident'])
                self.assertEqual(self.b.operations[op['id']]['error']['code'], code)

    def test_a_safe_refusal_restores_the_recorded_prior_flags_it_never_clears_more_than_it_set(self):
        # A quarantined board cannot start the diagnostic, so the recorded prior is normally clean; whatever it is, it wins.
        for prior in ({'quarantined': True, 'resident': True}, {'quarantined': True, 'resident': False}):
            with self.subTest(prior=prior):
                self.setUp()
                op = self.start()
                self.assertEqual(self.b.operations[op['id']]['diag_prior'], {'quarantined': False, 'resident': False})
                self.b.operations[op['id']]['diag_prior'] = prior
                self.b.finish('agent', {'id': op['id'], 'state': 'failed', 'error': self.refusal('diagnostic_stub_wrong_chip')})
                d = self.b.devices['p4']
                self.assertEqual((d['quarantined'], d['diagnostic_resident']), (prior['quarantined'], prior['resident']))

    def test_a_board_that_was_resident_cannot_start_the_diagnostic_so_no_refusal_can_lift_that_state(self):
        d = self.b.devices['p4']
        d['quarantined'], d['diagnostic_resident'] = True, True
        with self.assertRaises(Fault) as context:
            self.submit()
        self.assertEqual(context.exception.code, 'device_quarantined')
        self.assertTrue(d['quarantined'] and d['diagnostic_resident'])

    def test_the_safe_refusal_needs_the_whole_report_and_a_plain_failure(self):
        base = self.refusal('diagnostic_stub_wrong_chip')
        cases = {'ram_load started': ('failed', {**base, 'ram_load': 'started'}, False),
                 'ram_load unknown': ('failed', {**base, 'ram_load': 'unknown'}, False),
                 'no ram_load': ('failed', {k: v for k, v in base.items() if k != 'ram_load'}, False),
                 'wrong category': ('failed', {**base, 'category': 'ram_load_failed'}, False),
                 'origin differs': ('failed', {**base, 'origin_code': 'diagnostic_stub_not_identified'}, False),
                 'indeterminate state': ('indeterminate', base, False),
                 'hold flag': ('failed', base, True)}
        for name, (state, error, hold) in cases.items():
            with self.subTest(name):
                self.setUp()
                op = self.start()
                message = {'id': op['id'], 'state': state, 'error': error}
                if hold:
                    message['hold'] = True
                self.b.finish('agent', message)
                self.assertTrue(self.b.devices['p4']['quarantined'] and self.b.devices['p4']['diagnostic_resident'])

    def test_a_late_safe_refusal_after_the_deadline_never_clears_the_flags(self):
        op = self.start()
        self.b.operations[op['id']]['state'] = 'indeterminate'          # the execution deadline already passed
        self.b.finish('agent', {'id': op['id'], 'state': 'failed', 'error': self.refusal('diagnostic_stub_wrong_chip')})
        self.assertTrue(self.b.devices['p4']['quarantined'] and self.b.devices['p4']['diagnostic_resident'])

    def test_the_durable_state_survives_a_restart_in_both_outcomes(self):
        import tempfile
        for code, expected in (('diagnostic_stub_wrong_chip', (False, False)), ('diagnostic_session_state_indeterminate', (True, True))):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                self.setUp()
                from server.journal import Journal
                self.b.journal = Journal(tmp); self.b.journal.save(self.b)
                op = self.start()
                self.b.finish('agent', {'id': op['id'], 'state': 'failed', 'error': self.refusal(code)})
                self.b.agent_event('agent', {'type': 'settled', 'device': 'p4', 'generation': self.generation, 'id': op['id']})
                self.b.journal.close()
                again = Broker(state_dir=tmp)
                d = again.devices['p4']
                self.assertEqual((d['quarantined'], d['diagnostic_resident']), expected)
                again.journal.close()

    def test_an_agent_disconnection_during_the_run_leaves_the_board_resident_and_quarantined(self):
        self.start()
        self.b.disconnect('agent')
        self.assertTrue(self.b.devices['p4']['quarantined'] and self.b.devices['p4']['diagnostic_resident'])

    def test_a_reconnection_after_an_indeterminate_run_keeps_the_board_resident_and_quarantined(self):
        op = self.start()
        self.b.finish('agent', {'id': op['id'], 'state': 'indeterminate', 'error': {'code': 'hardware_outcome_unknown'}, 'hold': True})
        self.b.disconnect('agent')
        self.b.agent('agent', object())
        self.register('p4', 'esp32p4', 'native_usb_other')
        d = self.b.devices['p4']
        self.assertTrue(d['quarantined'] and d['diagnostic_resident'])
        with self.assertRaises(Fault) as context:
            self.b.submit('alice', ['read', 'control'], {'lease': self.b.reserve('p4', 'alice', 60)['id'],
                          'command': 'flash.read', 'args': {'address': 0x8000, 'size': 4096}})
        self.assertEqual(context.exception.code, 'device_quarantined')

    def test_losing_the_reservation_during_the_run_is_indeterminate_and_quarantines(self):
        op = self.start()
        self.b.release(self.lease['id'], 'alice', 'reservation_lost')
        self.assertEqual(self.b.operations[op['id']]['state'], 'indeterminate')
        self.assertTrue(self.b.devices['p4']['quarantined'])

    def test_an_execution_timeout_never_becomes_a_success(self):
        op = self.start()
        self.now += 10_000
        self.b.sweep()
        self.assertNotEqual(self.b.operations[op['id']]['state'], 'completed')

    def test_a_late_result_after_the_deadline_cannot_complete_the_operation(self):
        op = self.start()
        self.now += 10_000
        self.b.sweep()
        try:
            self.b.finish('agent', {'id': op['id'], 'state': 'completed', 'result': {'ok': True}})
        except Fault:
            pass
        self.assertNotEqual(self.b.operations[op['id']]['state'], 'completed')
        self.assertTrue(self.b.devices['p4']['quarantined'])

    def test_the_stub_artifact_stays_pinned_while_the_operation_is_active(self):
        self.submit()
        self.assertTrue(self.b.artifact_pinned(self.artifact['id']))


if __name__ == '__main__':
    unittest.main()


class UnknownFlashCapacityTests(unittest.TestCase):
    """ROM-only P4 session whose flash capacity is unknown: declared explicitly, never guessed, never reused."""
    def setUp(self):
        self.b = Broker(clock=lambda: 100., p4_stub_allowlist=[hashlib.sha256(stub_json()).hexdigest()])
        self.b.agent('agent', object())

    def register(self, **over):
        data = {'id': 'p4', 'chip': 'esp32p4', 'flash_size': None, 'flash_size_known': False, 'capabilities': ['identify', 'mark', CMD],
                'provenance': {'transport': 'native_usb_other', 'transport_basis': 'inferred_from_webserial_vid_pid'}}
        data.update(over)
        return self.b.register('agent', data, ['p4'])

    def refused(self, **over):
        with self.assertRaises(Fault) as context:
            self.register(**over)
        return context.exception.code

    def test_an_explicitly_unknown_capacity_registers_and_is_stored_as_unknown(self):
        self.register()
        d = self.b.devices['p4']
        self.assertIsNone(d['flash_size']); self.assertIs(d['flash_size_known'], False)

    def test_a_known_capacity_keeps_the_strict_integer_contract(self):
        self.register(flash_size=32 * 1024 * 1024, flash_size_known=True)
        self.assertEqual(self.b.devices['p4']['flash_size'], 32 * 1024 * 1024); self.assertIs(self.b.devices['p4']['flash_size_known'], True)

    def test_null_without_the_explicit_declaration_and_other_bad_sizes_are_refused(self):
        for over in ({'flash_size_known': None}, {'flash_size_known': True}, {'flash_size_known': 0}, {'flash_size': 0, 'flash_size_known': None},
                     {'flash_size': 1, 'flash_size_known': None}, {'flash_size': 'x', 'flash_size_known': None}, {'flash_size': True, 'flash_size_known': None}):
            with self.subTest(over=over):
                with self.assertRaises(Fault):
                    self.register(**over)
        self.assertNotIn('p4', self.b.devices)

    def test_an_unknown_capacity_must_be_null_and_is_limited_to_the_rom_only_p4(self):
        self.assertEqual(self.refused(flash_size=4096), 'invalid_request')                      # declared unknown but carries a value
        self.assertEqual(self.refused(chip='esp32c6'), 'invalid_request')
        self.assertEqual(self.refused(capabilities=['identify', 'mark', CMD, 'flash.read']), 'invalid_request')
        self.assertEqual(self.refused(capabilities=['identify', 'partitions']), 'invalid_request')

    def test_the_remembered_capacity_is_never_reused_by_a_new_registration(self):
        self.register(flash_size=32 * 1024 * 1024, flash_size_known=True)
        self.b.disconnect('agent'); self.b.agent('agent', object())
        self.register()
        self.assertIsNone(self.b.devices['p4']['flash_size'])

    def test_commands_that_depend_on_the_capacity_are_refused_while_it_is_unknown(self):
        self.register(capabilities=['identify', 'mark', CMD])
        d = self.b.devices['p4']; d['capabilities'] = sorted(CAPABILITIES_FOR_TEST)         # even if a stale capability list claimed them
        lease = self.b.reserve('p4', 'alice', 600)
        for command, args in (('flash.read', {'address': 0x8000, 'size': 4096}), ('partitions', {}), ('flash.erase', {'chip': 'esp32p4'}),
                              ('flash.write', {'chip': 'esp32p4', 'images': []})):
            with self.subTest(command=command), self.assertRaises(Fault) as context:
                self.b.submit('alice', ['read', 'control', 'program'], {'lease': lease['id'], 'command': command, 'args': args, 'allow_destructive': True})
            self.assertEqual(context.exception.code, 'flash_capacity_unknown')

    def test_identify_and_mark_do_not_need_the_capacity(self):
        self.register()
        lease = self.b.reserve('p4', 'alice', 600)
        for command, args in (('identify', {}), ('mark', {'label': 'x'})):
            op = self.b.submit('alice', ['read', 'control'], {'lease': lease['id'], 'command': command, 'args': args})
            self.assertEqual(op['state'], 'queued')

    def test_the_diagnostic_is_accepted_without_a_known_capacity(self):
        self.register()
        stub = stub_json(); art = self.b.artifact(stub, hashlib.sha256(stub).hexdigest(), 'alice')
        sha = art['sha256']; lease = self.b.reserve('p4', 'alice', 600); self.b.devices['p4']['grant_until'] = 10000
        args = {'chip': 'esp32p4', 'stub_artifact': art['id'], 'stub_sha256': sha, 'address': 0x8000, 'size': 1024 * 1024, 'packet_size': 4096,
                'cpu_hz': 200_000_000, 'consent': p4_trace_consent('p4', sha)}
        op = self.b.submit('alice', ['read', 'control', 'program'], {'lease': lease['id'], 'command': CMD, 'args': args, 'allow_destructive': True})
        self.assertEqual(op['state'], 'queued')

    def test_the_unknown_state_survives_a_restart(self):
        import tempfile
        from server.journal import Journal
        with tempfile.TemporaryDirectory() as tmp:
            self.b.journal = Journal(tmp)
            self.register(); self.b.journal.save(self.b); self.b.journal.close()
            again = Broker(state_dir=tmp)
            self.assertIsNone(again.devices['p4']['flash_size']); self.assertIs(again.devices['p4']['flash_size_known'], False)
            again.journal.close()


CAPABILITIES_FOR_TEST = {'identify', 'mark', CMD, 'flash.read', 'partitions', 'flash.erase', 'flash.write'}
