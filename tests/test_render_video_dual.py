import array
import contextlib
import csv
import io
import math
import subprocess
import tempfile
import unittest
import wave
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from image_render import render_common as common
from video_render import render_video as video
from video_render import render_video_dual as dual
from tests.test_render_video import write_png


class DualVideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)/'双人 % render'
        self.root.mkdir()
        self.ffmpeg = video.find_ffmpeg()
        self.output = self.root/'joined.mp4'

    def tearDown(self):
        self.tmp.cleanup()

    def sequences(self, first=0, count=25):
        sequences = []
        for side, color in [('left', (220, 10, 10)), ('right', (10, 10, 220))]:
            folder = self.root/side
            folder.mkdir()
            for row in range(first, first + count):
                write_png(folder/f'{row:06d}.png', (color[0], 10 + 3*(row - first), color[2]))
            seq = video.read_sequence(folder)
            seq['id'] = side
            sequences.append(seq)
        return sequences

    def wav(self, name, frequency, tone=1, silence=0, channels=1):
        samples = array.array('h', [0]*round(silence*48000)*channels)
        for index in range(round(tone*48000)):
            value = round(10000*math.sin(2*math.pi*frequency*index/48000))
            samples.extend([value]*channels)
        path = self.root/name
        with wave.open(str(path), 'wb') as output:
            output.setparams((channels, 2, 48000, 0, 'NONE', 'not compressed'))
            output.writeframes(samples.tobytes())
        return path

    def encode(self, sequences, wavs=(None, None), fps=25):
        with contextlib.redirect_stdout(io.StringIO()):
            return dual.encode_dual(sequences, Fraction(fps), wavs, self.output, self.ffmpeg)

    def channels(self):
        result = subprocess.run([self.ffmpeg, '-v', 'error', '-i', str(self.output), '-map', '0:a:0',
                                 '-c:a', 'pcm_s16le', '-f', 's16le', '-'], capture_output=True, check=True)
        samples = array.array('h')
        samples.frombytes(result.stdout)
        return samples[::2], samples[1::2]

    @staticmethod
    def amplitude(samples, frequency):
        return math.hypot(sum(value*math.cos(2*math.pi*frequency*i/48000) for i, value in enumerate(samples)),
                          sum(value*math.sin(2*math.pi*frequency*i/48000) for i, value in enumerate(samples)))/len(samples)

    def test_left_right_pixels_and_voice_channels_with_trim_and_padding(self):
        sequences = self.sequences()
        left = self.wav('左配音.wav', 480, tone=0.5, channels=2)
        right = self.wav('右配音.wav', 960, tone=2)
        self.encode(sequences, [left, right])
        result = subprocess.run([self.ffmpeg, '-v', 'error', '-i', str(self.output), '-map', '0:v:0',
                                 '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
                                capture_output=True, check=True)
        self.assertEqual(len(result.stdout), 25*64*32*3)
        lpos, rpos = (16*64 + 16)*3, (16*64 + 48)*3
        self.assertGreater(result.stdout[lpos], 200)
        self.assertLess(result.stdout[lpos + 2], 30)
        self.assertGreater(result.stdout[rpos + 2], 200)
        self.assertLess(result.stdout[rpos], 30)
        for frame in range(25):
            for position in [lpos, rpos]:
                green = result.stdout[frame*64*32*3 + position + 1]
                self.assertLessEqual(abs(green - (10 + 3*frame)), 4)
        lc, rc = self.channels()
        self.assertLessEqual(abs(len(lc) - 48000), 1024)
        for samples, own, other in [(lc[4800:14400], 480, 960), (rc[4800:14400], 960, 480)]:
            primary = self.amplitude(samples, own)
            self.assertGreater(primary, 3000)
            self.assertLess(self.amplitude(samples, other), primary*0.03)
        self.assertLess(max(abs(v) for v in lc[36000:45000]), 10)
        self.assertGreater(max(abs(v) for v in rc[36000:45000]), 5000)
        self.assertTrue(left.exists() and right.exists())
        for sequence in sequences:
            self.assertEqual(list(Path(sequence['output']).glob('*.png')), [])
        info = common.read_json(self.output.with_suffix('.json'))
        self.assertEqual(info['verification']['resolution'], [64, 32])
        self.assertEqual(info['verification']['audio_layout'], 'stereo')
        self.assertEqual(info['image_cleanup']['deleted_images'], 50)

    def test_missing_left_wav_gives_silent_left_and_nonzero_start_seeks_right(self):
        sequences = self.sequences(first=25)
        right = self.wav('right.wav', 960, tone=1, silence=1)
        self.encode(sequences, [None, right])
        lc, rc = self.channels()
        self.assertLessEqual(max(abs(v) for v in lc), 1)
        self.assertGreater(self.amplitude(rc[4800:14400], 960), 3000)

    def test_no_wavs_produces_silent_fractional_rate_video(self):
        self.encode(self.sequences(count=3), fps='30000/1001')
        info = common.read_json(self.output.with_suffix('.json'))
        self.assertEqual(info['verification']['decoded_frames'], 3)
        self.assertEqual(info['verification']['fps'], '30000/1001')
        self.assertFalse(info['verification']['audio_verified'])

    def test_verification_failure_keeps_both_png_sets(self):
        sequences = self.sequences(count=3)
        with patch.object(video, 'verify_video', side_effect=RuntimeError('invalid media')):
            with self.assertRaisesRegex(RuntimeError, 'invalid media'):
                self.encode(sequences)
        self.assertFalse(self.output.exists())
        for seq in sequences:
            self.assertEqual(len(list(Path(seq['output']).glob('*.png'))), 3)

    def test_changed_right_image_prevents_deletion_on_either_side(self):
        sequences = self.sequences(count=3)
        verifier = video.verify_video
        def changed_during_encoding(*args, **kwargs):
            result = verifier(*args, **kwargs)
            (Path(sequences[1]['output'])/'000001.png').write_bytes(b'changed')
            return result
        with patch.object(video, 'verify_video', side_effect=changed_during_encoding):
            with self.assertRaisesRegex(RuntimeError, 'Image changed'):
                self.encode(sequences)
        for seq in sequences:
            self.assertEqual(len(list(Path(seq['output']).glob('*.png'))), 3)

    def csv(self, name, count):
        path = self.root/name
        with path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(['Timecode', 'BlendShapeCount'] + common.CHANNELS)
            for row in range(count):
                writer.writerow([common.timecode(row), 61] + [0]*61)
        return str(path)

    def submit_args(self, left_count=3, right_count=3):
        return ['--csv1', self.csv('left.csv', left_count), '--csv2', self.csv('right.csv', right_count),
                '--avatar1', 'Bernice', '--avatar2', 'Stephane', '--output', str(self.root/'out')]

    def test_unequal_ranges_fail_before_render(self):
        args = self.submit_args(right_count=2)
        with patch.object(dual.render, 'enqueue_motion') as enqueue:
            with self.assertRaisesRegex(ValueError, 'row counts differ'):
                dual.main(args + ['--dry-run'])
        enqueue.assert_not_called()
        self.assertFalse((self.root/'out').exists())

    def test_dry_run_validates_two_avatars_and_common_range_without_writing(self):
        args = self.submit_args(right_count=2)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch.object(dual.render, 'enqueue_motion') as enqueue:
            self.assertEqual(dual.main(args + ['--count', '2', '--dry-run']), 0)
        self.assertIn('"fps": "25"', stdout.getvalue())
        self.assertIn('"total_images": 4', stdout.getvalue())
        enqueue.assert_not_called()
        self.assertFalse((self.root/'out').exists())

    def test_two_renders_complete_before_dual_encode(self):
        args = self.submit_args()
        events = []
        def enqueue(motion, preset, output, start_row, count, *, avatar):
            events.append(('render', avatar, preset, Path(motion['path']).name))
            return {'id': avatar, 'output': str(output)}
        def wait(jobs, timeout):
            events.append(('wait', jobs[0]['id']))
            return 0
        def encode(sequences, *args, **kwargs):
            events.append(('dual', len(sequences)))
        with patch.object(dual.render, 'require_service'), \
                patch.object(dual.render, 'enqueue_motion', side_effect=enqueue), \
                patch.object(dual.render, 'wait_for_jobs', side_effect=wait), \
                patch.object(video, 'read_sequence', side_effect=lambda path: {'output': path}), \
                patch.object(dual, 'encode_dual', side_effect=encode), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dual.main(args), 0)
        self.assertEqual(events, [('render', 'Bernice', 'split', 'left.csv'), ('wait', 'Bernice'),
                                  ('render', 'Stephane', 'redlantern', 'right.csv'), ('wait', 'Stephane'),
                                  ('dual', 2)])

    def test_second_render_failure_never_encodes_or_cleans_left_images(self):
        args = self.submit_args()
        with patch.object(dual.render, 'require_service'), \
                patch.object(dual.render, 'enqueue_motion', side_effect=[{'id': 'l', 'output': 'l'},
                                                                        {'id': 'r', 'output': 'r'}]), \
                patch.object(dual.render, 'wait_for_jobs', side_effect=[0, 1]), \
                patch.object(dual, 'encode_dual') as encode, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dual.main(args), 1)
        encode.assert_not_called()


if __name__ == '__main__':
    unittest.main()
