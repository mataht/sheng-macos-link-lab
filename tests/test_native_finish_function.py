"""Boundary checks for the opt-in read-only function return observer."""
import importlib.util
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'finish_function_observer', ROOT / 'UniXNU/scripts/qemu-entry/finish_function_observer.py')
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)
BASE = 0xfffffe0000000000


class ReadOnlyPackets:
    def __init__(self, values, *, pstate=5, thread=BASE + 0xa000, code='00' * 16):
        self.values = values
        self.pstate = pstate
        self.thread_reply = (struct.pack('<Q', thread).hex() if isinstance(thread, int) else thread)
        self.code = code
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        if request == 'g':
            return (struct.pack('<33Q', *self.values) + struct.pack('<I', self.pstate)).hex()
        if request == 'p1f':
            return self.thread_reply
        if request == f'm{self.values[32]:x},10':
            return self.code
        raise AssertionError(f'Unexpected or potentially mutating packet: {request}')


class FinishFunctionTests(unittest.TestCase):
    def setUp(self):
        self.values = [0] * 33
        self.values[30:33] = [BASE + 0x2000, BASE + 0x8000, BASE + 0x1000]
        self.plan = dict(entry=self.values[32], entry_bytes='00' * 16,
                         executable_ranges=[(BASE + 0x1000, BASE + 0x3000)])

    def entry(self, packet=None, values=None, interrupted=False):
        values = self.values if values is None else values
        return observer.capture_entry(packet or ReadOnlyPackets(values), values, 31, 47,
                                      self.plan, interrupted)

    def returned(self, entry=None, *, thread=BASE + 0xa000, stack=None, pc=None,
                 interrupted=False, pstate=5, changed_snapshot=False):
        values = list(self.values)
        values[0] = 1
        values[32] = self.values[30] if pc is None else pc
        if stack is not None:
            values[31] = stack
        snapshot = list(values)
        if changed_snapshot:
            snapshot[0] = 42
        return observer.return_observation(ReadOnlyPackets(snapshot, thread=thread, pstate=pstate), values,
                                           31, 47, entry or self.entry(), interrupted)

    def test_real_matching_entry_and_return(self):
        packet = ReadOnlyPackets(self.values)
        entry = self.entry(packet)
        self.assertTrue(entry['non_timeout'])
        self.assertTrue(observer.verified_return(self.returned(entry)))
        self.assertEqual(packet.calls, ['g', f'm{self.values[32]:x},10', 'p1f'])

    def test_timeout_el0_and_code_mismatch_rejected(self):
        for packet, interrupted in ((ReadOnlyPackets(self.values), True),
                                    (ReadOnlyPackets(self.values, pstate=0), False),
                                    (ReadOnlyPackets(self.values, code='01' * 16), False)):
            with self.subTest(interrupted=interrupted, pstate=packet.pstate, code=packet.code):
                with self.assertRaises(ValueError):
                    self.entry(packet, interrupted=interrupted)

    def test_wrong_pc_stack_and_caller_rejected(self):
        for index, replacement in ((32, BASE + 0x1004), (31, BASE + 0x8008),
                                   (31, 0x8000), (30, BASE + 0x4000), (30, BASE + 0x2002)):
            values = list(self.values)
            values[index] = replacement
            with self.subTest(index=index, replacement=replacement):
                with self.assertRaises(ValueError):
                    self.entry(ReadOnlyPackets(values), values)

    def test_changed_stopped_register_snapshot_rejected(self):
        changed = list(self.values)
        changed[0] = 42
        with self.assertRaises(ValueError):
            self.entry(ReadOnlyPackets(changed))

    def test_unknown_threads_never_match(self):
        for reply in ('E01', '', 'x' * 16, 0, BASE + 0xa001):
            with self.subTest(reply=reply):
                entry = self.entry(ReadOnlyPackets(self.values, thread=reply))
                result = self.returned(entry, thread=reply)
                self.assertIsNone(entry['thread'])
                self.assertFalse(result['thread_verified'])
                self.assertFalse(result['thread_matched'])
                self.assertFalse(observer.verified_return(result))
                self.assertFalse(observer.unrelated_return(result))

    def test_unreadable_return_thread_cannot_certify(self):
        result = self.returned(thread='E01')
        self.assertFalse(observer.verified_return(result))
        self.assertFalse(result['thread_verified'])

    def test_other_thread_or_stack_is_unrelated(self):
        for result in (self.returned(thread=BASE + 0xb000),
                       self.returned(stack=BASE + 0x9000)):
            self.assertTrue(observer.unrelated_return(result))
            self.assertFalse(observer.verified_return(result))

    def test_timeout_or_wrong_caller_never_verifies(self):
        self.assertFalse(observer.verified_return(self.returned(interrupted=True)))
        self.assertFalse(observer.verified_return(self.returned(pc=BASE + 0x2100)))

    def test_return_el0_and_changed_register_snapshot_never_verify(self):
        el0 = self.returned(pstate=0)
        changed = self.returned(changed_snapshot=True)
        self.assertFalse(el0['el1'])
        self.assertFalse(changed['register_snapshot_matched'])
        self.assertFalse(observer.verified_return(el0))
        self.assertFalse(observer.verified_return(changed))

    def test_missing_return_register_snapshot_never_verifies(self):
        values = list(self.values)
        values[32] = values[30]
        packet = ReadOnlyPackets(values)
        result = observer.return_observation(
            lambda request: 'E01' if request == 'g' else packet(request),
            values, 31, 47, self.entry(), False)
        self.assertFalse(result['register_snapshot_matched'])
        self.assertFalse(result['el1'])
        self.assertFalse(observer.verified_return(result))

    def test_absent_register_is_not_zero_thread(self):
        result = observer.thread_identity(ReadOnlyPackets(self.values), None, 47)
        self.assertIsNone(result['value'])
        self.assertIsNotNone(result['error'])


if __name__ == '__main__':
    unittest.main()
