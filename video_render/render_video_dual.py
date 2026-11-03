"""Render two CSV/avatar pairs into left/right video with separated stereo voices."""
import argparse
import json
import subprocess
import sys
import time
import uuid
from fractions import Fraction
from pathlib import Path

from image_render import render
from video_render import render_video as video
from image_render.render_common import atomic_json, config, read_motion, resolve_avatar


def paired_range(motions, start, count):
    if count is not None and count <= 0:
        raise ValueError('count must be positive')
    sizes = []
    for index, motion in enumerate(motions, 1):
        total = len(motion['records'])
        if not 0 <= start < total:
            raise ValueError(f'start-row is outside csv{index} ({total} rows)')
        sizes.append(min(count, total - start) if count is not None else total - start)
    if sizes[0] != sizes[1]:
        raise ValueError(f'CSV selected row counts differ: csv1={sizes[0]}, csv2={sizes[1]}. '
                         'Use --count to select the same range; no rows are silently dropped.')
    return sizes[0]


def paired_sequence(sequences):
    left, right = sequences
    if Path(left['output']).resolve() == Path(right['output']).resolve():
        raise ValueError('Use two different image directories')
    if (left['start_row'], left['end_row_exclusive'], left['expected_images']) != \
            (right['start_row'], right['end_row_exclusive'], right['expected_images']):
        raise ValueError('Left/right image row ranges must match')
    if left['resolution'][1] != right['resolution'][1]:
        raise ValueError('Left/right image heights must match')
    return {'expected_images': left['expected_images'],
            'resolution': [left['resolution'][0] + right['resolution'][0], left['resolution'][1]]}


def encode_dual(sequences, fps, wavs, output, ffmpeg=None, dry_run=False):
    combined = paired_sequence(sequences)
    output = video.absolute_path(output)
    metadata = output.with_suffix('.json')
    if output.suffix.lower() != '.mp4':
        raise ValueError('Video output must end in .mp4')
    if output.exists() or metadata.exists():
        raise FileExistsError(f'Output already exists: {output}')
    fps = Fraction(fps)
    duration = Fraction(combined['expected_images'], 1)/fps
    audio_start = Fraction(sequences[0]['start_row'], 1)/fps
    inputs = [{'side': side, 'avatar': seq.get('avatar'), 'source_job_id': seq.get('id'),
               'source_csv': seq.get('source_csv'), 'source_sha256': seq.get('source_sha256'),
               'images': seq['output'], 'wav': str(wav) if wav else None,
               'audio_channel': channel}
              for side, channel, seq, wav in zip(['left', 'right'], ['FL', 'FR'], sequences, wavs)]
    info = {'video': str(output), 'fps': str(fps), 'frames': combined['expected_images'],
            'resolution': combined['resolution'], 'duration_seconds': float(duration),
            'audio_start_seconds': float(audio_start), 'inputs': inputs,
            'delete_images_after_verification': True, 'dry_run': dry_run}
    print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
    if dry_run:
        return output
    ffmpeg = ffmpeg or video.find_ffmpeg()
    snapshots = [video.image_snapshot(seq) for seq in sequences]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name('.' + output.stem + '.' + uuid.uuid4().hex[:10] + '.partial.mp4')
    command = [ffmpeg, '-hide_banner', '-v', 'warning', '-nostdin', '-n']
    for seq in sequences:
        # Escape literal percent signs in directories for FFmpeg's image2 pattern.
        pattern = str(Path(seq['output'])).replace('%', '%%') + '/%06d.png'
        command += ['-framerate', str(fps), '-start_number', str(seq['start_row']),
                    '-start_number_range', '1', '-i', pattern]
    seconds = f'{float(duration):.12f}'
    filters = ['[0:v]setpts=PTS-STARTPTS,setsar=1[lvideo]',
               '[1:v]setpts=PTS-STARTPTS,setsar=1[rvideo]',
               '[lvideo][rvideo]hstack=inputs=2,pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p[v]']
    has_audio = any(wavs)
    if has_audio:
        input_index = 2
        for side, wav in zip(['left', 'right'], wavs):
            if wav:
                command += ['-i', str(wav)]
                filters.append(f'[{input_index}:a:0]atrim=start={float(audio_start):.12f},'
                               f'asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=mono,'
                               f'apad,atrim=duration={seconds}[{side}audio]')
                input_index += 1
            else:
                filters.append(f'anullsrc=r=48000:cl=mono,atrim=duration={seconds}[{side}audio]')
        filters.append('[leftaudio][rightaudio]join=inputs=2:channel_layout=stereo:'
                       'map=0.0-FL|1.0-FR[a]')
    command += ['-filter_complex', ';'.join(filters), '-map', '[v]',
                # hstack does not propagate a nominal frame rate. Set it at the
                # encoder so MP4 gives the last frame a complete 1/fps duration.
                '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-r', str(fps), '-fps_mode', 'cfr',
                '-frames:v', str(combined['expected_images'])]
    if has_audio:
        command += ['-map', '[a]', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2']
    else:
        command += ['-an']
    command += ['-t', seconds, '-movflags', '+faststart', str(temporary)]
    print('FFmpeg: avatar1 在左，avatar2 在右；wav1 左声道，wav2 右声道。', flush=True)
    try:
        result = subprocess.run(command, capture_output=True)
        if result.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError('Dual video encoding failed; both PNG sets retained: ' +
                               result.stderr.decode('utf-8', errors='replace')[-6000:])
        info['verification'] = video.verify_video(temporary, combined, fps, has_audio, ffmpeg,
                                                  expected_audio_layout='stereo' if has_audio else None)
        # Check both sets before deleting anything from either one.
        for snapshot, seq in zip(snapshots, sequences):
            video.check_image_snapshot(snapshot, seq['output'])
        temporary.rename(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    info['state'] = 'completed'
    info['image_cleanup'] = {'state': 'pending'}
    atomic_json(metadata, info)
    try:
        for index, (snapshot, seq, source) in enumerate(zip(snapshots, sequences, inputs), 1):
            side_info = {'video': str(output), 'frames': combined['expected_images'], 'fps': str(fps),
                         'source_job_id': seq.get('id'), 'side': source['side'], 'state': 'completed',
                         'verification': info['verification']}
            side_metadata = output.with_name(output.stem + f'.side{index}.json')
            try:
                video.cleanup_images(snapshot, side_info, side_metadata, seq['output'])
            finally:
                source['image_cleanup'] = side_info.get('image_cleanup', {'state': 'not_started'})
        info['image_cleanup'] = {'state': 'completed',
                                 'deleted_images': sum(s['image_cleanup']['deleted_images'] for s in inputs),
                                 'reclaimed_bytes': sum(s['image_cleanup']['reclaimed_bytes'] for s in inputs)}
    except (OSError, RuntimeError) as error:
        info['image_cleanup'] = {'state': 'failed', 'error': str(error)}
        raise
    finally:
        atomic_json(metadata, info)
    print(f'双人视频已保存: {output}', flush=True)
    return output


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    submit = commands.add_parser('submit', help='Render csv1/avatar1, then csv2/avatar2, then join video/audio')
    for index in [1, 2]:
        submit.add_argument(f'--csv{index}', type=Path, required=True, help='One CSV file')
        submit.add_argument(f'--avatar{index}', required=True)
    submit.add_argument('--output', type=Path, required=True, help='Output root directory')
    submit.add_argument('--start-row', type=int, default=0)
    submit.add_argument('--count', type=int)
    submit.add_argument('--timeout', type=float, default=0, help='Wait timeout for each avatar; 0 = unlimited')
    submit.add_argument('--wait', action='store_true', help='Accepted for compatibility; always waits')
    encode = commands.add_parser('encode', help='Join two existing final PNG directories, then delete their PNGs')
    encode.add_argument('--images1', type=Path, required=True, help='Left image directory')
    encode.add_argument('--images2', type=Path, required=True, help='Right image directory')
    encode.add_argument('--output', type=Path, required=True, help='Output MP4 filename')
    for command in [submit, encode]:
        command.add_argument('--fps', type=video.frame_rate, default=Fraction(25), help='Default: 25')
        command.add_argument('--wav1', type=Path, help='Left voice; omit for silent left channel')
        command.add_argument('--wav2', type=Path, help='Right voice; omit for silent right channel')
        command.add_argument('--dry-run', action='store_true')
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith('--') and argv[0] != '--help':
        argv.insert(0, 'submit')
    args = build_parser().parse_args(argv)
    ffmpeg = None if args.dry_run else video.find_ffmpeg()
    wavs = [video.checked_wav(args.wav1, ffmpeg), video.checked_wav(args.wav2, ffmpeg)]
    if args.command == 'encode':
        sequences = [video.read_sequence(args.images1), video.read_sequence(args.images2)]
        encode_dual(sequences, args.fps, wavs, args.output, ffmpeg, args.dry_run)
        return 0
    settings = config()
    routes = [resolve_avatar(args.avatar1, settings), resolve_avatar(args.avatar2, settings)]
    motions = [read_motion(args.csv1), read_motion(args.csv2)]
    count = paired_range(motions, args.start_row, args.count)
    summary = {'left': {'avatar': routes[0][0], 'preset': routes[0][1], 'csv': motions[0]['path']},
               'right': {'avatar': routes[1][0], 'preset': routes[1][1], 'csv': motions[1]['path']},
               'fps': str(args.fps), 'frames_per_avatar': count, 'total_images': 2*count,
               'duration_seconds': float(Fraction(count, 1)/args.fps), 'dry_run': args.dry_run}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        return 0
    render.require_service()
    task_id = time.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:10]
    root = video.absolute_path(args.output) / (routes[0][0] + '__' + routes[1][0]) / task_id
    root.mkdir(parents=True, exist_ok=False)
    task = dict(summary, id=task_id, state='rendering', jobs=[], video=str(root/'video_dual.mp4'))
    task_file = root/'dual_job.json'
    atomic_json(task_file, task)
    try:
        for side, motion, (avatar, preset) in zip(['left', 'right'], motions, routes):
            job = render.enqueue_motion(motion, preset, root/side, args.start_row, count, avatar=avatar)
            task['jobs'].append({'side': side, 'id': job['id'], 'output': job['output']})
            atomic_json(task_file, task)
            result = render.wait_for_jobs([job], args.timeout)
            if result:
                task['state'] = 'render_incomplete'
                task['failed_side'] = side
                return result
        task['state'] = 'encoding'
        atomic_json(task_file, task)
        sequences = [video.read_sequence(j['output']) for j in task['jobs']]
        encode_dual(sequences, args.fps, wavs, task['video'], ffmpeg)
        task['state'] = 'completed'
        return 0
    except (Exception, KeyboardInterrupt) as error:
        task['state'] = 'failed' if isinstance(error, Exception) else 'interrupted'
        task['error'] = str(error)
        raise
    finally:
        atomic_json(task_file, task)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('Stopped. Completed videos and any remaining PNGs have been kept.', file=sys.stderr)
        sys.exit(130)
