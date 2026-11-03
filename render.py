"""Submit CSV rendering jobs from ordinary Python, PowerShell, or an MCP caller."""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from render_common import (RUNTIME, atomic_json, config, prepare_job, read_json,
                           read_motion, resolve_avatar, avatar_for_preset, image_progress)

TERMINAL_STATES = {'completed', 'failed', 'cancelled', 'interrupted', 'superseded'}


def require_service():
    try:
        heartbeat = read_json(RUNTIME / 'heartbeat.json')
        if time.time() - heartbeat['time'] > 15:
            raise ValueError('stale heartbeat')
        if Path(heartbeat['project']).resolve() != Path(config()['project']).resolve():
            raise ValueError('wrong project')
    except (OSError, ValueError, KeyError):
        raise RuntimeError('Unreal render service is offline. Open MetaThomas and run start_service.py '
                           'from the Unreal Python menu/console. See README.md.')


def submit(args):
    cfg = config()
    avatar, preset = resolve_avatar(args.avatar, cfg)
    paths = []
    for value in args.csv:
        path = Path(value).resolve()
        paths.extend(sorted(path.rglob(args.pattern)) if path.is_dir() else [path])
    paths = list(dict.fromkeys(paths))
    if not paths:
        raise ValueError('No matching CSV files')
    motions = [read_motion(path) for path in paths]
    for motion in motions:
        if not 0 <= args.start_row < len(motion['records']):
            raise ValueError('start-row is outside ' + motion['path'])
    if args.count is not None and args.count <= 0:
        raise ValueError('count must be positive')
    summary = [{'csv': m['path'], 'rows': len(m['records']), 'sha256': m['sha256'],
                'selected_rows': min(args.count or len(m['records']), len(m['records'])-args.start_row)}
               for m in motions]
    print(json.dumps({'inputs': summary, 'avatar': avatar, 'preset': preset,
                      'jobs': len(motions),
                      'expected_images': sum(s['selected_rows'] for s in summary),
                      'dry_run': args.dry_run}, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        return 0
    require_service()
    jobs = []
    for motion in motions:
        job_id = time.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:10]
        job = prepare_job(motion, preset, args.output, job_id, args.start_row, args.count, avatar=avatar)
        atomic_json(RUNTIME / 'requests' / (job_id + '.json'), job)
        jobs.append(job)
        print(json.dumps({'submitted': job_id, 'avatar': avatar, 'preset': preset,
                          'expected_images': job['expected_images'], 'output': job['output']}, ensure_ascii=False), flush=True)
    if not args.wait:
        return 0
    return wait_for_jobs(jobs, args.timeout)


def current_status(job, progress=True):
    path = RUNTIME / 'status' / (job['id'] + '.json')
    status = read_json(path) if path.exists() else dict(job)
    if 'avatar' not in status:
        try:
            status['avatar'] = avatar_for_preset(status['preset'])
        except ValueError:
            status['avatar'] = status['preset']
    if progress:
        status.update(image_progress(status))
    return status


def wait_for_jobs(jobs, timeout=0):
    deadline = time.monotonic() + timeout if timeout else float('inf')
    last = {}
    next_progress = 0
    while True:
        statuses = []
        now = time.monotonic()
        refresh_progress = now >= next_progress
        for job in jobs:
            status = current_status(job, progress=False)
            statuses.append(status)
            if refresh_progress or last.get(job['id']) != status['state']:
                status.update(image_progress(status))
                print(f'[{status["avatar"]}] {status["state"]} | '
                      f'图片 {status["images_written"]}/{status["expected_images"]} '
                      f'({status["image_progress_percent"]:.2f}%) | {job["id"]}', flush=True)
                if status.get('error'):
                    print(status['error'], file=sys.stderr, flush=True)
                last[job['id']] = status['state']
        if refresh_progress:
            next_progress = now + 10
        if all(s['state'] in TERMINAL_STATES for s in statuses):
            return 0 if all(s['state'] == 'completed' for s in statuses) else 1
        if time.monotonic() >= deadline:
            print('Wait timed out. Jobs remain in Unreal; use status or cancel.', file=sys.stderr)
            return 2
        time.sleep(1)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    send = commands.add_parser('submit', help='Render CSV files (or a folder recursively)')
    send.add_argument('--csv', action='append', required=True, help='File or folder; repeat for multiple inputs')
    send.add_argument('--pattern', default='*.csv', help='Glob used when --csv is a directory')
    send.add_argument('--avatar', required=True, help='One avatar: Bernice or Stephane (case insensitive; BP_ aliases accepted)')
    send.add_argument('--output', type=Path, required=True)
    send.add_argument('--start-row', type=int, default=0, help='Zero-based CSV data row')
    send.add_argument('--count', type=int, help='Number of data rows; omit to render to the end')
    send.add_argument('--dry-run', action='store_true', help='Validate CSVs and print image counts without writing jobs')
    send.add_argument('--wait', action='store_true')
    send.add_argument('--timeout', type=float, default=0, help='Wait timeout in seconds; 0 means unlimited')
    status = commands.add_parser('status')
    status.add_argument('job_id', nargs='?')
    status.add_argument('--watch', action='store_true', help='Watch actual image counts for one job')
    cancel = commands.add_parser('cancel')
    cancel.add_argument('job_id')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'submit':
        return submit(args)
    if args.command == 'cancel':
        if not args.job_id.replace('_', '').isalnum():
            raise ValueError('Invalid job id')
        atomic_json(RUNTIME / 'cancel' / (args.job_id + '.json'), {'id': args.job_id})
        print('Cancellation requested: ' + args.job_id)
        return 0
    if args.job_id and not args.job_id.replace('_', '').isalnum():
        raise ValueError('Invalid job id')
    if args.watch:
        if not args.job_id:
            parser.error('status --watch requires a JOB_ID')
        paths = [RUNTIME / 'status' / (args.job_id + '.json'),
                 RUNTIME / 'requests' / (args.job_id + '.json')]
        path = next((p for p in paths if p.exists()), None)
        if path is None:
            raise ValueError('Unknown job: ' + args.job_id)
        return wait_for_jobs([read_json(path)])
    paths = [RUNTIME / 'status' / (args.job_id + '.json')] if args.job_id else sorted((RUNTIME/'status').glob('*.json'))
    print(json.dumps([current_status(read_json(p)) for p in paths], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
