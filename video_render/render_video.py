"""Render CSVs then encode MP4, or encode an existing numbered PNG directory."""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import uuid
from fractions import Fraction
from pathlib import Path

from image_render import render
from image_render.render_common import atomic_json, read_json, validate_images


def frame_rate(value):
    try:
        rate = Fraction(value)
        if rate <= 0 or max(rate.numerator, rate.denominator) > 2147483647:
            raise ValueError()
        return rate
    except (ValueError, ZeroDivisionError, OverflowError):
        raise argparse.ArgumentTypeError('fps must be positive, e.g. 25, 30, 29.97 or 30000/1001')


def absolute_path(path):
    return Path(os.path.abspath(str(path)))


def find_ffmpeg():
    executable = shutil.which(os.environ.get('FFMPEG_EXE', 'ffmpeg'))
    if not executable:
        raise RuntimeError('FFmpeg not found. Add ffmpeg.exe to PATH or set FFMPEG_EXE to its full path.')
    return executable


def checked_wav(path, ffmpeg=None):
    if path is None:
        return None
    path = absolute_path(path)
    if not path.is_file():
        raise FileNotFoundError(f'Audio file not found: {path}')
    if ffmpeg:
        # Check decoding before spending time on Unreal rendering.
        result = subprocess.run([ffmpeg, '-hide_banner', '-v', 'error', '-nostdin',
                                 '-i', str(path), '-map', '0:a:0', '-t', '0.01',
                                 '-f', 'null', '-'], capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError('Cannot decode audio: ' + result.stderr.decode('utf-8', errors='replace'))
    return path


def read_sequence(folder):
    folder = absolute_path(folder)
    if not folder.is_dir() or folder.name == '.capture':
        raise ValueError(f'Use a completed PNG output directory, not .capture: {folder}')
    job_file = folder/'job.json'
    if job_file.exists():
        job = read_json(job_file)
        if job['state'] != 'completed':
            raise ValueError(f'Image job is {job["state"]}; wait for completed before encoding.')
        sequence = dict(job, output=str(folder))
    else:
        paths = sorted(folder.glob('*.png'))
        if not paths or any(not p.stem.isdecimal() or p.name != f'{int(p.stem):06d}.png' for p in paths):
            raise ValueError('Expected consecutive PNGs named 000000.png, 000001.png, ...')
        numbers = sorted(int(p.stem) for p in paths)
        if numbers[-1] - numbers[0] + 1 != len(numbers):
            raise ValueError('Missing PNG frames; refusing to create a truncated video.')
        with paths[0].open('rb') as handle:
            header = handle.read(24)
        if len(header) != 24 or header[:8] != b'\x89PNG\r\n\x1a\n':
            raise ValueError(f'Invalid PNG: {paths[0]}')
        sequence = {'output': str(folder), 'start_row': numbers[0],
                    'end_row_exclusive': numbers[-1] + 1, 'expected_images': len(numbers),
                    'resolution': list(struct.unpack('>II', header[16:24]))}
    count = validate_images(sequence)
    if count <= 0 or count != sequence['expected_images']:
        raise ValueError('Image count does not match the render job.')
    return sequence


def image_snapshot(sequence):
    """Resolve the exact files used by this video, never a recursive delete target."""
    folder = Path(sequence['output']).resolve(strict=True)
    snapshots = []
    for row in range(sequence['start_row'], sequence['end_row_exclusive']):
        path = folder/f'{row:06d}.png'
        if path.is_symlink() or path.resolve(strict=True).parent != folder or not path.is_file():
            raise ValueError(f'PNG must be a regular file inside the image directory: {path}')
        stat = path.stat()
        snapshots.append((path, (stat.st_size, stat.st_mtime_ns, stat.st_ino)))
    if len(snapshots) != sequence['expected_images']:
        raise ValueError('Image range does not match expected_images')
    return snapshots


def verify_video(path, sequence, fps, has_audio, ffmpeg, expected_audio_layout=None):
    """Fully decode before deleting PNGs; verify every video frame and audio stream."""
    command = [ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-xerror',
               '-err_detect', 'explode', '-i', str(path), '-map', '0:v:0']
    if has_audio:
        command += ['-map', '0:a:0']
    command += ['-fps_mode', 'passthrough', '-f', 'framecrc', '-']
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise RuntimeError('Video verification failed; PNGs retained: ' +
                           result.stderr.decode('utf-8', errors='replace')[-6000:])
    timebases, packets = {}, {0: [], 1: []}
    dimensions, audio_layout = None, None
    for line in result.stdout.decode('utf-8', errors='replace').splitlines():
        if line.startswith('#tb '):
            stream, value = line[4:].split(':', 1)
            timebases[int(stream)] = Fraction(value.strip())
        elif line.startswith('#dimensions 0:'):
            dimensions = [int(n) for n in line.split(':', 1)[1].strip().split('x')]
        elif line.startswith('#channel_layout_name 1:'):
            audio_layout = line.split(':', 1)[1].strip()
        elif line and not line.startswith('#'):
            fields = line.split(',')
            stream, pts, duration = int(fields[0]), int(fields[2]), int(fields[3])
            packets[stream].append((pts, duration))
    expected = sequence['expected_images']
    if dimensions != [size + size % 2 for size in sequence['resolution']]:
        raise RuntimeError('Video dimensions do not match the image layout; PNGs retained.')
    if timebases.get(0) != 1/fps or packets[0] != [(i, 1) for i in range(expected)]:
        raise RuntimeError(f'Video frame count/timing mismatch; expected {expected} frames at {fps} fps. '
                           f'Decoded {len(packets[0])} frames, time base {timebases.get(0)}, '
                           f'first/last packets {packets[0][:2]}/{packets[0][-2:]}. PNGs retained.')
    duration = Fraction(expected, 1)/fps
    if has_audio:
        audio = packets[1]
        if not audio or 1 not in timebases:
            raise RuntimeError('Audio is missing or empty; PNGs retained.')
        audio_end = (audio[-1][0] + audio[-1][1]) * timebases[1]
        # AAC decoding can include padding of one 1024-sample frame at 48 kHz.
        if audio[0][0] != 0 or abs(audio_end - duration) > Fraction(1024, 48000):
            raise RuntimeError('Audio duration does not match the video; PNGs retained.')
        if expected_audio_layout is not None and audio_layout != expected_audio_layout:
            raise RuntimeError(f'Expected {expected_audio_layout} audio, got {audio_layout}; PNGs retained.')
    return {'decoded_frames': expected, 'fps': str(fps), 'duration_seconds': float(duration),
            'resolution': dimensions, 'audio_verified': bool(has_audio), 'audio_layout': audio_layout}


def check_image_snapshot(snapshots, image_folder):
    # Recheck every file before deleting the first one. Do not delete PNGs that
    # were replaced/edited while FFmpeg was encoding, or follow redirected paths.
    folder = Path(image_folder).resolve(strict=True)
    for path, before in snapshots:
        if path.is_symlink() or path.resolve(strict=True).parent != folder:
            raise RuntimeError(f'Image path changed during encoding; PNGs retained: {path}')
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != before:
            raise RuntimeError(f'Image changed during encoding; PNGs retained: {path}')


def cleanup_images(snapshots, info, metadata, image_folder):
    check_image_snapshot(snapshots, image_folder)
    folder = Path(image_folder).resolve(strict=True)
    cleanup = {'state': 'deleting', 'deleted_images': 0, 'reclaimed_bytes': 0}
    info['image_cleanup'] = cleanup
    # A receipt stays with the images even when --output points elsewhere.
    receipt = folder/'image_cleanup.json'
    atomic_json(metadata, info)
    atomic_json(receipt, info)
    try:
        for path, before in snapshots:
            path.unlink()
            cleanup['deleted_images'] += 1
            cleanup['reclaimed_bytes'] += before[0]
        cleanup['state'] = 'completed'
        cleanup['finished_at'] = time.time()
    except OSError as error:
        cleanup['state'] = 'failed'
        cleanup['error'] = str(error)
        raise RuntimeError(f'Video saved, but PNG cleanup failed after {cleanup["deleted_images"]} files: {error}')
    finally:
        atomic_json(metadata, info)
        atomic_json(receipt, info)
    print(f'已清理 {cleanup["deleted_images"]} 张 PNG，释放 '
          f'{cleanup["reclaimed_bytes"]/1024/1024:.2f} MiB', flush=True)


def encode_video(sequence, fps, wav=None, output=None, ffmpeg=None, dry_run=False):
    folder = Path(sequence['output'])
    output = absolute_path(output) if output is not None else folder/'video.mp4'
    if output.suffix.lower() != '.mp4':
        raise ValueError('Video output must end in .mp4')
    metadata = output.with_suffix('.json')
    if output.exists() or metadata.exists():
        raise FileExistsError(f'Output already exists; use encode --output with a new filename: {output}')
    duration = Fraction(sequence['expected_images'], 1) / fps
    audio_start = Fraction(sequence['start_row'], 1) / fps
    info = {'video': str(output), 'images': str(folder), 'frames': sequence['expected_images'],
            'fps': str(fps), 'duration_seconds': float(duration),
            'wav': str(wav) if wav else None,
            'audio_start_seconds': float(audio_start) if wav else None,
            'source_job_id': sequence.get('id'), 'delete_images_after_verification': True,
            'dry_run': dry_run}
    print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
    if dry_run:
        return output
    ffmpeg = ffmpeg or find_ffmpeg()
    snapshots = image_snapshot(sequence)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name('.' + output.stem + '.' + uuid.uuid4().hex[:10] + '.partial.mp4')
    # Run in the image directory so '%' characters in directory names cannot
    # accidentally become part of image2's filename pattern.
    command = [ffmpeg, '-hide_banner', '-v', 'warning', '-nostdin', '-n',
               '-framerate', str(fps), '-start_number', str(sequence['start_row']),
               '-start_number_range', '1', '-i', '%06d.png']
    if wav:
        command += ['-i', str(wav)]
    command += ['-map', '0:v:0', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
                '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p', '-fps_mode', 'passthrough',
                '-frames:v', str(sequence['expected_images'])]
    seconds = f'{float(duration):.12f}'
    if wav:
        # Keep all images even when the WAV is shorter; do not use -shortest.
        command += ['-map', '1:a:0', '-af',
                    f'atrim=start={float(audio_start):.12f},asetpts=PTS-STARTPTS,'
                    f'apad,atrim=duration={seconds}',
                    '-c:a', 'aac', '-b:a', '192k', '-ar', '48000']
    else:
        command += ['-an']
    command += ['-t', seconds, '-movflags', '+faststart', str(temporary)]
    print(f'FFmpeg: {sequence["expected_images"]} images -> {output}', flush=True)
    try:
        result = subprocess.run(command, cwd=str(folder), capture_output=True)
        if result.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
            error = result.stderr.decode('utf-8', errors='replace')
            raise RuntimeError(f'FFmpeg failed ({result.returncode}): {error[-6000:]}')
        print('正在校验视频帧数、时间及音频…', flush=True)
        info['verification'] = verify_video(temporary, sequence, fps, bool(wav), ffmpeg)
        # Windows rename refuses to replace an existing result.
        temporary.rename(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    info['state'] = 'completed'
    atomic_json(metadata, info)
    print(f'视频已保存: {output}', flush=True)
    cleanup_images(snapshots, info, metadata, folder)
    return output


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    submit = commands.add_parser('submit', help='For each CSV: render, encode/verify MP4, then delete PNGs')
    render.add_submit_arguments(submit)
    encode = commands.add_parser('encode', help='Encode/verify existing PNGs, then delete those PNGs')
    encode.add_argument('images', type=Path, help='Directory containing completed numbered PNGs')
    encode.add_argument('--output', type=Path, help='MP4 filename; default: images/video.mp4')
    encode.add_argument('--dry-run', action='store_true')
    for command in [submit, encode]:
        command.add_argument('--fps', type=frame_rate, default=Fraction(25),
                             help='Video frame rate (default: 25); e.g. 25 or 30000/1001')
        command.add_argument('--wav', type=Path, help='Optional audio; omit for silent video')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    ffmpeg = None if args.dry_run else find_ffmpeg()
    wav = checked_wav(args.wav, ffmpeg)
    if args.command == 'encode':
        encode_video(read_sequence(args.images), args.fps, wav, args.output, ffmpeg, args.dry_run)
        return 0
    if args.dry_run:
        list(render.iter_jobs(args))
        print(json.dumps({'video_fps': str(args.fps), 'wav': str(wav) if wav else None,
                          'video_filename': 'video.mp4', 'delete_images_after_verification': True,
                          'dry_run': True}, ensure_ascii=False))
        return 0
    # Submit the next CSV only after the previous video's PNGs have been removed.
    for job in render.iter_jobs(args):
        result = render.wait_for_jobs([job], args.timeout)
        if result:
            print('Image rendering did not complete successfully. No video encoded or PNGs deleted; '
                  'use encode on the completed image directory to retry.', file=sys.stderr)
            return result
        try:
            encode_video(read_sequence(job['output']), args.fps, wav, ffmpeg=ffmpeg)
        except (OSError, ValueError, RuntimeError) as error:
            print(f'{job["id"]}: {error}', file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('Stopped. Completed videos and any remaining PNGs have been kept.', file=sys.stderr)
        sys.exit(130)
