"""Read-only packet boundary tests for the same-run GOP instance observer."""
import importlib.util
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'UniXNU/scripts/qemu-entry'))
SPEC = importlib.util.spec_from_file_location('gop_instance_observer',
    ROOT / 'UniXNU/scripts/qemu-entry/gop_instance_observer.py')
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)
B = 0xfffffe0000000000


class Fixture:
    def __init__(self):
        self.instance = B + 0x100000
        self.vtable = B + 0x10000
        self.natural, self.ret, self.halt = B + 0x4000, B + 0x5000, B + 0x6000
        self.values = [0] * 33
        self.values[0], self.values[31], self.values[32] = 1, B + 0x200000, self.ret
        self.pstate, self.now = 5, 0
        self.calls, self.memory_reads, self.installs = [], [], {}
        self.install_failure = self.restore_failure = None
        self.changed_vptr = self.regressed = False
        self.bool_value = 1
        self.fields = {name: dict(offset=7128 + i * 8, bytes=8, type='uint64_t')
                       for i, name in enumerate(observer.COUNTERS)}
        self.fields['fDisplayOn'] = dict(offset=7120, bytes=1, type='bool')
        self.guards = [dict(symbol=s, address=B + 0x1000 + i * 0x100, entry_bytes='11' * 16)
                       for i, s in enumerate(('stop', 'D0', 'D1'))]
        self.plan = dict(window_seconds=90, total_timeout=180,
            natural_el1=dict(address=self.natural, entry_bytes='22' * 16),
            layout=dict(class_bytes=7504, kernel_va_bits=47, vtable_address_point=self.vtable,
                        fields=self.fields, guards=self.guards))
        self.entry = dict(arguments=[hex(self.instance)])
        self.returned = dict(raw_x0='0x1', **{k: True for k in (
            'caller_matched', 'stack_matched', 'thread_verified', 'thread_matched',
            'non_timeout', 'register_snapshot_matched', 'el1')})
        self.events = [dict(pc=B + 0x7000, time=90, interrupted=True, pstate=0),
                       dict(pc=self.natural, time=91, interrupted=False, pstate=5)]

    def packet(self, request):
        self.calls.append(request)
        if request == 'g':
            return (struct.pack('<33Q', *self.values) + struct.pack('<I', self.pstate)).hex()
        if request == 's':
            self.values[32] += 4
            return 'T05'
        if request.startswith(('Z1,', 'z1,')):
            address = int(request.split(',')[1], 16)
            if request.startswith('Z1'):
                self.installs[address] = self.installs.get(address, 0) + 1
                if address == self.install_failure or (
                        address == self.restore_failure and self.installs[address] > 1):
                    return 'E01'
            return 'OK'
        if request.startswith('m'):
            address, size = (int(x, 16) for x in request[1:].split(','))
            self.memory_reads.append((address, size, self.now))
            if address == self.natural and size == 16:
                return '22' * 16
            if address in {g['address'] for g in self.guards} and size == 16:
                return '11' * 16
            if address == self.instance and size == 8:
                v = self.vtable + (8 if self.changed_vptr and self.now >= 90 else 0)
                return v.to_bytes(8, 'little').hex()
            for name, field in self.fields.items():
                if address == self.instance + field['offset'] and size == field['bytes']:
                    value = self.bool_value if field['type'] == 'bool' else (
                        10 if self.now < 90 else 9 if self.regressed else 12)
                    return value.to_bytes(size, 'little').hex()
            return 'E01'
        raise AssertionError(f'Unexpected or mutating packet: {request}')

    def resume(self, until=None):
        if not self.events:
            raise AssertionError('Unexpected resume after event budget')
        event = self.events.pop(0)
        self.now = event['time']
        self.values = list(self.values)
        self.values[32] = event['pc']
        self.values[0] = event.get('x0', self.values[0])
        self.pstate = event.get('pstate', 5)
        return 'T05', tuple(self.values), event.get('interrupted', False)

    def run(self, deadline=180):
        return observer.observe(self.packet, self.resume, 'T05', tuple(self.values), False,
            deadline, self.plan, self.entry, self.returned, self.ret, self.halt,
            clock=lambda: self.now)[3]

    def after_field_reads(self):
        return [r for r in self.memory_reads if r[2] >= 90 and
                self.instance < r[0] < self.instance + 7504]


class GopInstanceTests(unittest.TestCase):
    def test_el0_then_natural_el1_complete(self):
        f = Fixture(); result = f.run()
        self.assertTrue(result['complete'], result)
        self.assertEqual(len(result['snapshots']), 2)
        self.assertEqual(result['counter_deltas']['values']['fFrames'], 2)
        self.assertIn(f'Z1,{f.natural:x},4', f.calls)

    def test_already_el1_does_not_resume_to_natural(self):
        f = Fixture()
        f.events = [dict(pc=B + 0x7000, time=90, interrupted=True, pstate=5)]
        result = f.run()
        self.assertTrue(result['complete'], result)
        self.assertNotIn(f'Z1,{f.natural:x},4', f.calls)

    def test_same_instance_lifecycle_permanently_invalidates(self):
        for index in range(3):
            with self.subTest(guard=index):
                f = Fixture()
                f.events = [dict(pc=f.guards[index]['address'], time=90,
                                 x0=f.instance, interrupted=False)]
                result = f.run()
                self.assertTrue(result['lifecycle_invalidated'])
                self.assertFalse(result['complete'])
                self.assertNotIn('after_window', result['snapshots'])
                self.assertEqual(f.after_field_reads(), [])

    def test_guard_install_and_restore_failure(self):
        f = Fixture(); f.install_failure = f.guards[1]['address']
        self.assertFalse(f.run()['complete'])
        f = Fixture(); f.restore_failure = f.guards[0]['address']
        f.events.insert(0, dict(pc=f.guards[0]['address'], time=1, x0=f.instance + 0x10000))
        result = f.run()
        self.assertFalse(result['complete'])
        self.assertIn('restore', result['error'])

    def test_unrelated_guard_skipped_and_bounded(self):
        f = Fixture()
        f.events.insert(0, dict(pc=f.guards[0]['address'], time=1, x0=f.instance + 0x10000))
        self.assertTrue(f.run()['complete'])
        f = Fixture()
        f.events = [dict(pc=f.guards[0]['address'], time=i + 1, x0=f.instance + 0x10000)
                    for i in range(17)]
        result = f.run()
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['guard_events']), 17)

    def test_deadline_and_natural_timeout(self):
        f = Fixture(); result = f.run(deadline=99)
        self.assertFalse(result['complete'])
        self.assertIn('deadline', result['error'])
        f = Fixture(); f.events[1] = dict(pc=f.natural, time=180, interrupted=True)
        result = f.run()
        self.assertFalse(result['complete'])
        self.assertNotIn('after_window', result['snapshots'])

    def test_changed_vptr_rejected_without_endpoint_fields(self):
        f = Fixture(); f.changed_vptr = True
        result = f.run()
        self.assertFalse(result['complete'])
        self.assertEqual(f.after_field_reads(), [])

    def test_bad_field_bounds_and_noncanonical_bool(self):
        f = Fixture(); f.fields['fFrames']['offset'] = 7500
        result = f.run(); self.assertFalse(result['complete'])
        self.assertIn('extent', result['error'])
        f = Fixture(); f.bool_value = 2
        result = f.run(); self.assertFalse(result['complete'])
        self.assertIn('noncanonical', result['error'])

    def test_counter_regression_not_complete(self):
        f = Fixture(); f.regressed = True
        result = f.run()
        self.assertFalse(result['complete'])
        self.assertIn('fFrames', result['counter_deltas']['regressions'])

    def test_d0_d1_same_address_deduplicated(self):
        f = Fixture(); f.guards[2]['address'] = f.guards[1]['address']
        result = f.run()
        self.assertTrue(result['complete'], result)
        self.assertEqual(len(result['deduplicated_guard_addresses']), 2)
        self.assertEqual(f.installs[f.guards[1]['address']], 1)

    def test_other_el_at_window_end_rejected(self):
        f = Fixture()
        f.events = [dict(pc=B + 0x7000, time=90, interrupted=True, pstate=9)]
        self.assertFalse(f.run()['complete'])


if __name__ == '__main__':
    unittest.main()
