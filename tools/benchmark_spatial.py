"""Compare the first three CSV rows at 1/2/4/8 spatial samples in an idle UE editor.

Uses the existing renderer without reloading Unreal. Temporarily changes only
spatial_samples and restores the exact original config after every job.
"""
import argparse
import datetime
import json
import re
import statistics
import time
import uuid
from pathlib import Path

from image_render import render
from image_render import render_common as common


def duration(value):
    hours, minutes, seconds = value.split(':')
    return 3600 * int(hours) + 60 * int(minutes) + float(seconds)


def log_metrics(text):
    result = {}
    patterns = {
        'mrq_pipeline_seconds': r'Movie Pipeline completed\. Duration: \+(\d+:\d+:\d+\.\d+)',
        'mrq_executor_seconds': r'MoviePipelineLinearExecutorBase finished 1 jobs in \+(\d+:\d+:\d+\.\d+)',
    }
    for key, pattern in patterns.items():
        values = re.findall(pattern, text)
        if len(values) != 1:
            raise RuntimeError(f'Expected one timing for {key}, got {values}')
        result[key] = duration(values[0])
    # This interval includes the 50 engine and 32 render warm-up samples.
    # It is not an isolated GPU timing for the three saved images.
    events = {}
    for line in text.splitlines():
        for label, marker in [('cut_start', 'Finished initializing Camera Cut'),
                              ('cut_end', 'Finished processing Camera Cut')]:
            if marker in line:
                stamp = re.search(r'^\[([\d.]+-[\d.:]+)\]', line)
                if stamp:
                    events[label] = datetime.datetime.strptime(stamp[1], '%Y.%m.%d-%H.%M.%S:%f')
    if len(events) == 2:
        result['shot_including_warmup_seconds'] = (events['cut_end'] - events['cut_start']).total_seconds()
    return result


def wait_idle(timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        render.require_service()
        heartbeat = common.read_json(common.RUNTIME/'heartbeat.json')
        if not heartbeat['active_job'] and not list((common.RUNTIME/'requests').glob('*.json')):
            return
        time.sleep(0.2)
    raise RuntimeError('Unreal is busy; benchmark will not modify sampling for another job')


def run_one(motion, folder, log_path, baseline_bytes, settings, samples, repetition, warmup=False,
            reference_image=None):
    wait_idle()
    config_path = common.ROOT/'render_config.json'
    if config_path.read_bytes() != baseline_bytes:
        raise RuntimeError('Configuration changed outside benchmark; stopping without overwriting it')
    selected = dict(settings, spatial_samples=samples)
    common.atomic_json(config_path, selected)
    # Path.write_text uses Windows CRLF translation. Compare the actual bytes
    # written, not an LF-only serialization, when deciding whether to restore.
    config_bytes = config_path.read_bytes()
    if json.loads(config_bytes) != selected:
        raise RuntimeError('Configuration changed while selecting sample count')
    offset = log_path.stat().st_size
    run_name = 'warmup' if warmup else f'round_{repetition}'
    job_id = time.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:10]
    clock_start = time.perf_counter()
    observed_rendering_at = None
    try:
        job = common.prepare_job(motion, 'redlantern', folder/run_name/f'samples_{samples}',
                                 job_id, start=0, count=3, avatar='Stephane')
        # The existing UE service reads the same config in build_job(). Keep its
        # complete snapshot in the request for auditability without changing UE.
        job['benchmark_settings'] = selected
        common.atomic_json(common.RUNTIME/'requests'/(job_id+'.json'), job)
        print(f'START {run_name} samples={samples} id={job_id}', flush=True)
        deadline = time.monotonic() + 180
        status_path = common.RUNTIME/'status'/(job_id+'.json')
        while time.monotonic() < deadline:
            if status_path.exists():
                status = common.read_json(status_path)
                if status['state'] == 'rendering' and observed_rendering_at is None:
                    observed_rendering_at = time.time()
                if status['state'] in render.TERMINAL_STATES:
                    break
            time.sleep(0.2)
        else:
            raise RuntimeError(f'Benchmark job timed out: {job_id}; inspect its status before continuing')
        elapsed = time.perf_counter() - clock_start
        if status['state'] != 'completed':
            raise RuntimeError(status.get('error', status['state']))
        if common.validate_images(status) != 3:
            raise RuntimeError('Expected three rendered images')
        framing_mae = None
        if reference_image:
            from PIL import Image
            import numpy as np
            reference = np.asarray(Image.open(reference_image).convert('RGB').resize((32,32)), dtype=float)
            actual = np.asarray(Image.open(Path(status['output'])/'000000.png').convert('RGB').resize((32,32)), dtype=float)
            framing_mae = float(np.abs(reference-actual).mean())
            if framing_mae > 12:
                raise RuntimeError(f'Framing changed (thumbnail MAE {framing_mae:.2f}); check Unreal camera/window state')
        with log_path.open('rb') as handle:
            handle.seek(offset)
            log = handle.read().decode('utf-8', errors='replace')
        log_file = folder/f'{run_name}_samples_{samples}.log'
        log_file.write_text(log, encoding='utf-8')
        metrics = log_metrics(log)
        result = dict(samples=samples, repetition=repetition, warmup=warmup,
                      job_id=job_id, output=status['output'], actor_class=status['actor_class'],
                      end_to_end_seconds=elapsed,
                      service_seconds=status['finished_at']-status['started_at'],
                      observed_rendering_at=observed_rendering_at,
                      framing_thumbnail_mae=framing_mae,
                      log=str(log_file), **metrics)
        print('DONE '+json.dumps({k: result[k] for k in ['samples','repetition','end_to_end_seconds',
                                 'mrq_pipeline_seconds','shot_including_warmup_seconds']}, ensure_ascii=False), flush=True)
        return result
    finally:
        # Compare-and-restore: do not overwrite an unrelated user edit.
        if config_path.read_bytes() == config_bytes:
            temporary = config_path.with_suffix('.benchmark_restore.tmp')
            temporary.write_bytes(baseline_bytes)
            temporary.replace(config_path)
        else:
            raise RuntimeError('Configuration was edited during benchmark; leaving that edit intact')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference-image', type=Path, help='Known correct first-row framing; stop on large changes')
    parser.add_argument('--resume', action='store_true', help='Complete missing measured runs in the same output folder')
    args = parser.parse_args()
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=args.resume)
    config_path = common.ROOT/'render_config.json'
    baseline = config_path.read_bytes()
    if args.resume:
        if (folder/'original_render_config.json').read_bytes() != baseline:
            raise RuntimeError('Cannot resume with a changed render configuration')
    else:
        (folder/'original_render_config.json').write_bytes(baseline)
    settings = common.config()
    log_path = Path(settings['project']).parent/'Saved'/'Logs'/'MetaThomas.log'
    motion = common.read_motion(args.csv)
    if len(motion['records']) < 3:
        raise ValueError('Need at least three rows')
    orders = [[1, 4, 8, 2], [8, 2, 1, 4], [2, 8, 4, 1]]
    report = {'source_csv': motion['path'], 'source_sha256': motion['sha256'],
              'csv_rows': [0,1,2], 'avatar': 'Stephane', 'baseline_config': settings,
              'reference_image': str(args.reference_image) if args.reference_image else None,
              'orders': orders, 'runs': [],
              'timing_notes': {'end_to_end_seconds':'submission through image verification; includes preparation, startup, warmup and teardown',
                               'mrq_pipeline_seconds':'engine-reported MRQ duration; includes warmup and image output, excludes asset preparation',
                               'shot_including_warmup_seconds':'log interval from initialized camera cut to finished camera cut; includes warmup; not pure GPU time'}}
    if args.resume:
        previous = common.read_json(folder/'benchmark.json')
        if previous['source_sha256'] != report['source_sha256'] or previous['reference_image'] != report['reference_image']:
            raise RuntimeError('Cannot resume with different input or framing reference')
        report = previous
    else:
        common.atomic_json(folder/'benchmark.json', report)
    if 'warmup' not in report:
        report['warmup'] = run_one(motion, folder, log_path, baseline, settings, 4, 0, warmup=True,
                                 reference_image=args.reference_image)
    for repetition, order in enumerate(orders, 1):
        for samples in order:
            if any(r['samples'] == samples and r['repetition'] == repetition for r in report['runs']):
                continue
            report['runs'].append(run_one(motion, folder, log_path, baseline, settings, samples, repetition,
                                         reference_image=args.reference_image))
            common.atomic_json(folder/'benchmark.json', report)
    report['summary'] = []
    for samples in [1,2,4,8]:
        runs = [r for r in report['runs'] if r['samples'] == samples]
        row = {'samples': samples, 'runs': len(runs)}
        for key in ['end_to_end_seconds','mrq_pipeline_seconds','shot_including_warmup_seconds']:
            values = [r[key] for r in runs]
            row[key] = {'median':statistics.median(values),'min':min(values),'max':max(values)}
        report['summary'].append(row)
    report['config_restored_exactly'] = config_path.read_bytes() == baseline
    common.atomic_json(folder/'benchmark.json', report)
    print(json.dumps(report['summary'], indent=2), flush=True)
    print('REPORT '+str(folder/'benchmark.json'), flush=True)


if __name__ == '__main__':
    main()
