import array
import contextlib
import io
import math
import struct
import subprocess
import tempfile
import unittest
import wave
import zlib
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from video_render import render_video as video


def write_png(path, color):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    pixels = (b'\0' + bytes(color) * 32) * 32
    path.write_bytes(b'\x89PNG\r\n\x1a\n' +
                     chunk(b'IHDR', struct.pack('>IIBBBBB', 32, 32, 8, 2, 0, 0, 0)) +
                     chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)/'中文 images % test'
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def sequence(self, first=0, count=12):
        for index in range(first, first + count):
            write_png(self.root/f'{index:06d}.png', (index % 255, 40, 100))
        return video.read_sequence(self.root)

    def ffmpeg(self):
        try:
            return video.find_ffmpeg()
        except RuntimeError:
            self.skipTest('FFmpeg is required for media integration tests')

    def decoded_frames(self, output):
        result = subprocess.run([self.ffmpeg(), '-v', 'error', '-i', str(output),
                                 '-map', '0:v:0', '-f', 'framemd5', '-'], capture_output=True, check=True)
        lines = result.stdout.decode().splitlines()
        return [line for line in lines if line and not line.startswith('#')], lines

    def decoded_audio(self, output):
        result = subprocess.run([self.ffmpeg(), '-v', 'error', '-i', str(output),
                                 '-map', '0:a:0', '-ac', '1', '-ar', '48000',
                                 '-c:a', 'pcm_s16le', '-f', 's16le', '-'], capture_output=True, check=True)
        samples = array.array('h')
        samples.frombytes(result.stdout)
        return samples

    def audio(self, silence, tone):
        wav = self.root/'配音 sample.wav'
        data = array.array('h', [0] * round(silence * 48000))
        data.extend(round(12000 * math.sin(2 * math.pi * 480 * i / 48000))
                    for i in range(round(tone * 48000)))
        with wave.open(str(wav), 'wb') as handle:
            handle.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            handle.writeframes(data.tobytes())
        return wav

    def encode(self, sequence, fps, wav=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return video.encode_video(sequence, Fraction(fps), wav, ffmpeg=self.ffmpeg())

    def test_silent_fractional_fps_preserves_all_frames(self):
        output = self.encode(self.sequence(), '30000/1001')
        frames, headers = self.decoded_frames(output)
        self.assertEqual(len(frames), 12)
        self.assertIn('#tb 0: 1001/30000', headers)
        result = subprocess.run([self.ffmpeg(), '-v', 'error', '-i', str(output),
                                 '-map', '0:a:0', '-f', 'null', '-'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)  # no unwanted silent audio track

    def test_nonzero_start_aligns_wav_and_short_audio_pads_to_all_images(self):
        sequence = self.sequence(first=12, count=12)
        wav = self.audio(silence=1, tone=0.25)
        output = self.encode(sequence, 12, wav)
        samples = self.decoded_audio(output)
        self.assertLessEqual(abs(len(samples) - 48000), 1024)
        first_rms = math.sqrt(sum(v*v for v in samples[1000:5000])/4000)
        last_rms = math.sqrt(sum(v*v for v in samples[30000:45000])/15000)
        self.assertGreater(first_rms, 3000)  # sought past the first second of silence
        self.assertLess(last_rms, 10)  # short WAV padded; no dropped video frames
        self.assertEqual(len(self.decoded_frames(output)[0]), 12)

    def test_long_audio_trimmed_to_video_duration(self):
        output = self.encode(self.sequence(count=6), 30, self.audio(silence=0, tone=2))
        samples = self.decoded_audio(output)
        self.assertLessEqual(abs(len(samples) - 9600), 1024)
        self.assertEqual(len(self.decoded_frames(output)[0]), 6)

    def test_audio_before_selected_range_becomes_silence(self):
        output = self.encode(self.sequence(first=30, count=6), 30, self.audio(silence=0, tone=0.1))
        samples = self.decoded_audio(output)
        self.assertLessEqual(abs(len(samples) - 9600), 1024)
        self.assertLessEqual(max(abs(v) for v in samples), 1)

    def test_missing_frame_refused_and_existing_video_preserved(self):
        sequence = self.sequence(count=3)
        (self.root/'000001.png').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing PNG'):
            video.read_sequence(self.root)
        output = self.root/'video.mp4'
        output.write_bytes(b'existing result')
        with self.assertRaises(FileExistsError):
            video.encode_video(sequence, Fraction(30), ffmpeg=self.ffmpeg())
        self.assertEqual(output.read_bytes(), b'existing result')

    def test_fps_validation(self):
        parser = video.build_parser()
        for value in ['0', '-30', 'nan', 'inf', '1/0']:
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(['encode', str(self.root), '--fps', value])
        self.assertEqual(video.frame_rate('29.97'), Fraction(2997, 100))

    def test_submit_waits_then_encodes_without_wait_flag(self):
        self.sequence()
        with patch.object(video.render, 'iter_jobs', return_value=[{'output': str(self.root)}]) as enqueue, \
                patch.object(video.render, 'wait_for_jobs', return_value=0) as wait, \
                patch.object(video, 'encode_video') as encode:
            result = video.main(['submit', '--avatar', 'Stephane', '--csv', 'source.csv',
                                 '--output', str(self.root)])
        self.assertEqual(result, 0)
        self.assertEqual(enqueue.call_args.args[0].avatar, 'Stephane')
        wait.assert_called_once()
        self.assertEqual(encode.call_args.args[1], Fraction(25))

    def test_failed_render_never_encodes_partial_frames(self):
        with patch.object(video.render, 'iter_jobs', return_value=[{'output': str(self.root)}]), \
                patch.object(video.render, 'wait_for_jobs', return_value=1), \
                patch.object(video, 'encode_video') as encode, contextlib.redirect_stderr(io.StringIO()):
            result = video.main(['submit', '--avatar', 'Bernice', '--csv', 'source.csv',
                                 '--output', str(self.root)])
        self.assertEqual(result, 1)
        encode.assert_not_called()

    def test_bad_audio_rejected_before_render_submission(self):
        wav = self.root/'bad.wav'
        wav.write_bytes(b'not audio')
        with patch.object(video.render, 'iter_jobs') as enqueue:
            with self.assertRaisesRegex(ValueError, 'Cannot decode audio'):
                video.main(['submit', '--avatar', 'Bernice', '--csv', 'source.csv',
                            '--output', str(self.root), '--wav', str(wav)])
        enqueue.assert_not_called()

    def test_success_deletes_only_encoded_pngs_and_keeps_inputs_and_records(self):
        sequence = self.sequence(first=5, count=3)
        protected = ['source.csv', 'frame_mapping.json', 'keep.txt']
        for name in protected:
            (self.root/name).write_text('retain', encoding='utf-8')
        wav = self.audio(silence=0, tone=1)
        nested = self.root/'other'
        nested.mkdir()
        write_png(nested/'000005.png', (100, 50, 25))
        output = self.encode(sequence, 25, wav)
        self.assertTrue(output.is_file())
        self.assertEqual(list(self.root.glob('*.png')), [])
        self.assertTrue(wav.is_file())
        self.assertTrue((nested/'000005.png').is_file())
        for name in protected:
            self.assertEqual((self.root/name).read_text(encoding='utf-8'), 'retain')
        info = video.read_json(output.with_suffix('.json'))
        self.assertEqual(info['verification']['decoded_frames'], 3)
        self.assertEqual(info['image_cleanup']['state'], 'completed')
        self.assertEqual(info['image_cleanup']['deleted_images'], 3)
        self.assertGreater(info['image_cleanup']['reclaimed_bytes'], 0)

    def test_verification_failure_keeps_every_png(self):
        sequence = self.sequence(count=3)
        with patch.object(video, 'verify_video', side_effect=RuntimeError('verification failed')):
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                self.encode(sequence, 25)
        self.assertEqual(len(list(self.root.glob('*.png'))), 3)
        self.assertFalse((self.root/'video.mp4').exists())

    def test_encoder_failure_keeps_every_png(self):
        sequence = self.sequence(count=3)
        failure = subprocess.CompletedProcess([], 1, b'', b'encoder error')
        with patch.object(video.subprocess, 'run', return_value=failure):
            with self.assertRaisesRegex(RuntimeError, 'FFmpeg failed'):
                self.encode(sequence, 25)
        self.assertEqual(len(list(self.root.glob('*.png'))), 3)

    def test_dry_run_uses_25_fps_and_does_not_delete(self):
        self.sequence(count=3)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(video.main(['encode', str(self.root), '--dry-run']), 0)
        self.assertIn('"fps": "25"', stdout.getvalue())
        self.assertEqual(len(list(self.root.glob('*.png'))), 3)
        self.assertFalse((self.root/'video.mp4').exists())

    def test_changed_png_aborts_cleanup_before_deleting_any_file(self):
        sequence = self.sequence(count=3)
        snapshots = video.image_snapshot(sequence)
        (self.root/'000001.png').write_bytes(b'new data')
        with self.assertRaisesRegex(RuntimeError, 'Image changed'):
            video.cleanup_images(snapshots, {}, self.root/'video.json', self.root)
        self.assertEqual(len(list(self.root.glob('*.png'))), 3)

    def test_actual_verifier_rejects_frame_loss(self):
        sequence = self.sequence(count=3)
        output = self.encode(sequence, 25)
        wrong_sequence = dict(sequence, expected_images=4)
        with self.assertRaisesRegex(RuntimeError, 'frame count/timing mismatch'):
            video.verify_video(output, wrong_sequence, Fraction(25), False, self.ffmpeg())

    def test_batch_finishes_one_video_before_submitting_next_csv(self):
        events = []
        def submit_one_at_a_time(args):
            for index in range(2):
                events.append(f'submit-{index}')
                yield {'id': str(index), 'output': str(index)}
        def finish_video(sequence, *args, **kwargs):
            events.append(f'encode-and-clean-{sequence}')
        with patch.object(video.render, 'iter_jobs', side_effect=submit_one_at_a_time), \
                patch.object(video.render, 'wait_for_jobs', return_value=0), \
                patch.object(video, 'read_sequence', side_effect=lambda folder: folder), \
                patch.object(video, 'encode_video', side_effect=finish_video):
            result = video.main(['submit', '--avatar', 'Bernice', '--csv', 'folder',
                                 '--output', str(self.root)])
        self.assertEqual(result, 0)
        self.assertEqual(events, ['submit-0', 'encode-and-clean-0', 'submit-1', 'encode-and-clean-1'])

    def test_completed_job_status_reports_deleted_images(self):
        sequence = self.sequence(count=3)
        sequence.update(id='status_test', state='completed', preset='split', avatar='Bernice')
        self.encode(sequence, 25)
        stdout = io.StringIO()
        with patch.object(video.render, 'RUNTIME', self.root/'runtime'), contextlib.redirect_stdout(stdout):
            self.assertEqual(video.render.wait_for_jobs([sequence]), 0)
        self.assertIn('PNG 已清理 3/3', stdout.getvalue())
        self.assertNotIn('图片 0/3', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
