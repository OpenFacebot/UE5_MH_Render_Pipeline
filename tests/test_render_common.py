import csv
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_render import render_common as common
from image_render import render as cli


class CsvPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'motion.csv'
        # Deliberately reverse channel order: values must follow names, not positions.
        self.channels = list(reversed(common.CHANNELS))
        with self.source.open('w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Timecode', 'BlendshapeCount'] + self.channels + ['idx'])
            for row in range(3):
                writer.writerow([common.timecode(row), 61] +
                                [str(common.CHANNELS.index(c) / 100 + row / 1000) for c in self.channels] + [f'original-{row}'])

    def tearDown(self):
        self.tmp.cleanup()

    def test_reorders_channels_and_preserves_original(self):
        before = self.source.read_bytes()
        motion = common.read_motion(self.source)
        self.assertEqual(motion['sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual([float(v) for v in motion['records'][0]['values']], [i/100 for i in range(61)])
        self.assertEqual(self.source.read_bytes(), before)

    def test_atomic_status_write_survives_transient_windows_reader_lock(self):
        destination = self.root/'status.json'
        common.atomic_json(destination, {'state':'rendering'})
        replace = Path.replace
        attempts = []
        def temporarily_locked(source, target):
            attempts.append(source)
            if len(attempts) == 1:
                self.assertEqual(common.read_json(target), {'state':'rendering'})
                raise PermissionError('Windows reader still has destination open')
            return replace(source, target)
        with patch.object(Path, 'replace', autospec=True, side_effect=temporarily_locked), \
                patch.object(common.time, 'sleep'):
            common.atomic_json(destination, {'state':'completed','verified_images':3})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(common.read_json(destination), {'state':'completed','verified_images':3})
        self.assertFalse(destination.with_suffix('.json.tmp').exists())

    def test_last_row_has_one_image_and_original_idx(self):
        motion = common.read_motion(self.source)
        output = self.root / 'new_output'
        with patch.object(common, 'RUNTIME', self.root / 'runtime'):
            job = common.prepare_job(motion, 'split', output, 'unique', start=2, count=1)
        self.assertTrue(Path(job['output']).is_absolute())
        self.assertEqual(job['end_row_exclusive'], 3)
        self.assertEqual(job['expected_images'], 1)
        mapping = common.read_json(Path(job['output'])/'frame_mapping.json')
        self.assertEqual(mapping[0]['file'], '000002.png')
        self.assertEqual(mapping[0]['csv_line_number'], 4)
        self.assertEqual(mapping[0]['idx'], 'original-2')
        with Path(job['staged_csv']).open(newline='', encoding='utf-8') as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), 63)
        self.assertEqual(rows[1][0], '00:00:02:00.000')
        self.assertNotIn('idx', rows[0])

    def test_invalid_range_rejected_before_output(self):
        motion = common.read_motion(self.source)
        with self.assertRaises(ValueError):
            common.prepare_job(motion, 'split', self.root/'out', 'invalid', start=3)
        self.assertFalse((self.root/'out').exists())

    def test_non_finite_rejected(self):
        text = self.source.read_text(encoding='utf-8').replace('0.6,', 'nan,', 1)
        self.source.write_text(text, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Non-finite'):
            common.read_motion(self.source)

    def test_missing_channel_rejected(self):
        text = self.source.read_text(encoding='utf-8').replace('JawOpen', 'IncorrectName', 1)
        self.source.write_text(text, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Missing columns'):
            common.read_motion(self.source)

    def test_duplicate_output_refused(self):
        motion = common.read_motion(self.source)
        with patch.object(common, 'RUNTIME', self.root / 'runtime'):
            common.prepare_job(motion, 'split', self.root/'out', 'same', count=1)
            with self.assertRaises(FileExistsError):
                common.prepare_job(motion, 'split', self.root/'out', 'same', count=1)

    def test_settled_capture_frames_map_to_rows(self):
        folder = self.root / 'render'
        raw = folder / '.capture'
        raw.mkdir(parents=True)
        # The odd frames are the settled samples for CSV rows 2 and 3.
        (raw/'000005.png').write_bytes(b'row-two')
        (raw/'000007.png').write_bytes(b'row-three')
        common.finalize_capture({'output':str(folder), 'start_row':2, 'end_row_exclusive':4})
        self.assertEqual((folder/'000002.png').read_bytes(), b'row-two')
        self.assertEqual((folder/'000003.png').read_bytes(), b'row-three')

    def test_wrong_capture_phase_rejected_without_moving(self):
        folder = self.root / 'render'
        raw = folder / '.capture'
        raw.mkdir(parents=True)
        (raw/'000004.png').write_bytes(b'wrong-phase')
        with self.assertRaises(ValueError):
            common.finalize_capture({'output':str(folder), 'start_row':2, 'end_row_exclusive':3})
        self.assertTrue((raw/'000004.png').exists())

    def test_submit_requires_explicit_avatar(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main(['submit', '--csv', str(self.source), '--output', str(self.root/'out')])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse((self.root/'out').exists())

    def test_unknown_avatar_rejected_before_submission(self):
        with patch.object(cli, 'require_service') as service:
            with self.assertRaisesRegex(ValueError, 'supported avatar'):
                cli.main(['submit', '--avatar', 'Unknown', '--csv', str(self.source),
                          '--output', str(self.root/'out')])
        service.assert_not_called()
        self.assertFalse((self.root/'out').exists())

    def test_each_avatar_submits_only_its_scene(self):
        for requested, avatar, preset in [('bp_bernice', 'Bernice', 'split'),
                                           ('STEPHANE', 'Stephane', 'redlantern')]:
            with self.subTest(avatar=avatar):
                runtime = self.root / avatar / 'runtime'
                with patch.object(common, 'RUNTIME', runtime), patch.object(cli, 'RUNTIME', runtime), \
                        patch.object(cli, 'require_service'), contextlib.redirect_stdout(io.StringIO()):
                    result = cli.main(['submit', '--avatar', requested, '--csv', str(self.source),
                                       '--output', str(self.root/'out'), '--start-row', '1', '--count', '2'])
                self.assertEqual(result, 0)
                requests = list((runtime/'requests').glob('*.json'))
                self.assertEqual(len(requests), 1)
                job = common.read_json(requests[0])
                self.assertEqual((job['avatar'], job['preset']), (avatar, preset))
                self.assertEqual(job['expected_images'], 2)
                self.assertEqual(Path(job['output']).parent.name, avatar)
                mapping = common.read_json(Path(job['output'])/'frame_mapping.json')
                self.assertEqual([row['csv_row_index'] for row in mapping], [1, 2])

    def test_batch_dry_run_has_one_job_per_csv_and_writes_nothing(self):
        (self.root/'second.csv').write_bytes(self.source.read_bytes())
        stdout = io.StringIO()
        with patch.object(cli, 'require_service') as service, contextlib.redirect_stdout(stdout):
            result = cli.main(['submit', '--avatar', 'Bernice', '--csv', str(self.root),
                               '--output', str(self.root/'out'), '--dry-run'])
        summary = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual((summary['avatar'], summary['preset']), ('Bernice', 'split'))
        self.assertEqual((summary['jobs'], summary['expected_images']), (2, 6))
        service.assert_not_called()
        self.assertFalse((self.root/'out').exists())

    def test_avatar_scene_mismatch_rejected_before_output(self):
        with self.assertRaisesRegex(ValueError, 'uses preset'):
            common.prepare_job(common.read_motion(self.source), 'split', self.root/'out',
                               'mismatch', avatar='Stephane')
        self.assertFalse((self.root/'out').exists())

    def test_image_progress_counts_csv_rows_during_finalization(self):
        folder = self.root/'render'
        raw = folder/'.capture'
        raw.mkdir(parents=True)
        # Row 2 appears in both locations during finalization; count it once.
        for name in ['000005.png', '000007.png', '000006.png', '000099.png']:
            (raw/name).write_bytes(b'image')
        for name in ['000002.png', '000007.png', 'preview.png']:
            (folder/name).write_bytes(b'image')
        progress = common.image_progress({'output': str(folder), 'start_row': 2,
                                          'end_row_exclusive': 4, 'expected_images': 2})
        self.assertEqual(progress, {'images_written': 2, 'expected_images': 2,
                                    'image_progress_percent': 100.0})


if __name__ == '__main__':
    unittest.main()
