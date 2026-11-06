"""Only the two admitted same-run observation duration pairs may execute."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_native_gop_instance import Fixture


def preset_fixture(window, total, *, begin=0):
    fixture = Fixture()
    fixture.plan['window_seconds'] = window
    fixture.plan['total_timeout'] = total
    fixture.now = begin
    fixture.events[0]['time'] = begin + window
    fixture.events[1]['time'] = begin + window + 1
    return fixture


class GopInstancePresetTests(unittest.TestCase):
    def test_default_short_pair_preserves_ninety_second_window(self):
        fixture = Fixture()
        result = fixture.run()
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['window_seconds'], 90)
        self.assertEqual(result['window_elapsed_seconds'], 90)
        self.assertEqual(set(result['snapshots']), {'after_start', 'after_window'})

    def test_explicit_long_pair_observes_full_six_hundred_seconds(self):
        fixture = preset_fixture(600, 720)
        result = fixture.run(deadline=720)
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['window_seconds'], 600)
        self.assertEqual(result['window_elapsed_seconds'], 600)
        self.assertTrue(result['lifecycle_verified'])
        self.assertEqual(set(result['snapshots']), {'after_start', 'after_window'})
        self.assertEqual(result['counter_deltas']['values']['fFrames'], 2)

    def test_mismatched_pairs_rejected_before_resume(self):
        for window, total in ((90, 720), (600, 180)):
            with self.subTest(window=window, total=total):
                fixture = preset_fixture(window, total)
                result = fixture.run(deadline=720)
                self.assertFalse(result['complete'])
                self.assertFalse(result['lifecycle_verified'])
                self.assertIn('error', result)
                self.assertEqual(len(fixture.events), 2)

    def test_unapproved_pairs_rejected_before_resume(self):
        for window, total in ((120, 180), (600, 721), (601, 720), (90, 181),
                              (0, 180), (-90, 180), (720, 720), (600, 900)):
            with self.subTest(window=window, total=total):
                fixture = preset_fixture(window, total)
                result = fixture.run(deadline=900)
                self.assertFalse(result['complete'])
                self.assertIn('error', result)
                self.assertEqual(len(fixture.events), 2)

    def test_long_pair_retains_original_deadline_headroom(self):
        # 111 seconds have already elapsed; only 609 remain in the 720 deadline.
        fixture = preset_fixture(600, 720, begin=111)
        result = fixture.run(deadline=720)
        self.assertFalse(result['complete'])
        self.assertIn('deadline', result['error'])
        self.assertEqual(len(fixture.events), 2)

    def test_short_pair_retains_original_deadline_headroom(self):
        fixture = preset_fixture(90, 180, begin=81)
        result = fixture.run(deadline=180)
        self.assertFalse(result['complete'])
        self.assertIn('deadline', result['error'])
        self.assertEqual(len(fixture.events), 2)

    def test_exact_ten_second_headroom_is_sufficient(self):
        for window, total, begin in ((90, 180, 80), (600, 720, 110)):
            with self.subTest(window=window, total=total):
                fixture = preset_fixture(window, total, begin=begin)
                result = fixture.run(deadline=total)
                self.assertTrue(result['complete'], result)
                self.assertEqual(result['window_elapsed_seconds'], window)


if __name__ == '__main__':
    unittest.main()
