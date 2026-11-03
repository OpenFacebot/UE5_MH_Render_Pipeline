"""CSV preparation and on-disk job protocol. Runs in Python 3.9+ without Unreal."""
import csv
import hashlib
import json
import math
import os
import re
import struct
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
CHANNELS = ('EyeBlinkLeft EyeLookDownLeft EyeLookInLeft EyeLookOutLeft EyeLookUpLeft '
            'EyeSquintLeft EyeWideLeft EyeBlinkRight EyeLookDownRight EyeLookInRight '
            'EyeLookOutRight EyeLookUpRight EyeSquintRight EyeWideRight JawForward '
            'JawRight JawLeft JawOpen MouthClose MouthFunnel MouthPucker MouthRight '
            'MouthLeft MouthSmileLeft MouthSmileRight MouthFrownLeft MouthFrownRight '
            'MouthDimpleLeft MouthDimpleRight MouthStretchLeft MouthStretchRight '
            'MouthRollLower MouthRollUpper MouthShrugLower MouthShrugUpper MouthPressLeft '
            'MouthPressRight MouthLowerDownLeft MouthLowerDownRight MouthUpperUpLeft '
            'MouthUpperUpRight BrowDownLeft BrowDownRight BrowInnerUp BrowOuterUpLeft '
            'BrowOuterUpRight CheekPuff CheekSquintLeft CheekSquintRight NoseSneerLeft '
            'NoseSneerRight TongueOut HeadYaw HeadPitch HeadRoll LeftEyeYaw LeftEyePitch '
            'LeftEyeRoll RightEyeYaw RightEyePitch RightEyeRoll').split()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    # Windows readers can briefly hold the destination without delete sharing.
    # Retry the atomic replacement so status polling cannot fail a finished render.
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(0.01 * 2**attempt, 0.1))


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def config():
    return read_json(ROOT / 'render_config.json')


def resolve_avatar(name, settings=None):
    """Resolve one explicitly selected avatar to one scene preset."""
    settings = config() if settings is None else settings
    avatars = settings.get('avatars', {})
    requested = str(name or '').strip().casefold()
    matches = [(canonical, route['preset']) for canonical, route in avatars.items()
               if requested and requested in
               [n.casefold() for n in [canonical] + route.get('aliases', [])]]
    if len(matches) != 1:
        raise ValueError(f'Choose one supported avatar: {", ".join(avatars)}. Received: {name!r}')
    canonical, preset = matches[0]
    if preset not in settings['presets']:
        raise ValueError(f'Avatar {canonical} references an unknown preset: {preset}')
    return canonical, preset


def avatar_for_preset(preset, settings=None):
    """Identify avatars in older job records that only stored a preset."""
    settings = config() if settings is None else settings
    names = [name for name, route in settings.get('avatars', {}).items()
             if route['preset'] == preset]
    if len(names) != 1:
        raise ValueError(f'Preset {preset} does not identify one avatar; specify an avatar explicitly')
    return names[0]


def timecode(index):
    minutes, seconds = divmod(index, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}:00.000'


def read_motion(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    # Read and hash the same snapshot so changing source files cannot change a queued job.
    import io
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''))
    header = reader.fieldnames or []
    if len(header) != len(set(header)):
        raise ValueError(f'Duplicate CSV headers: {path}')
    missing = set(CHANNELS + ['Timecode']) - set(header)
    if missing:
        raise ValueError(f'Missing columns in {path}: {sorted(missing)}')
    count_col = next((h for h in header if h.lower() == 'blendshapecount'), None)
    if count_col is None:
        raise ValueError(f'Missing BlendShapeCount in {path}')
    records = []
    for index, row in enumerate(reader):
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'Wrong CSV column count: {path}, data row {index}')
        if int(row[count_col]) != 61:
            raise ValueError(f'Expected 61 coefficients: {path}, data row {index}')
        values = [row[name].strip() for name in CHANNELS]
        if not all(math.isfinite(float(v)) for v in values):
            raise ValueError(f'Non-finite coefficients: {path}, data row {index}')
        records.append({'row': index, 'idx': row.get('idx', str(index)),
                        'timecode': row['Timecode'], 'values': values})
    if not records:
        raise ValueError(f'Empty CSV: {path}')
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'records': records}


def prepare_job(motion, preset, output_root, job_id, start=0, count=None, *, avatar=None):
    settings = config()
    # Keep older Python callers compatible while the public CLI requires --avatar.
    avatar = avatar_for_preset(preset, settings) if avatar is None else avatar
    avatar, selected_preset = resolve_avatar(avatar, settings)
    if selected_preset != preset:
        raise ValueError(f'Avatar {avatar} uses preset {selected_preset}, not {preset}')
    records = motion['records']
    if start < 0 or start >= len(records):
        raise ValueError(f'start-row {start} is outside {motion["path"]} ({len(records)} rows)')
    if count is not None and count <= 0:
        raise ValueError('count must be positive')
    end = len(records) if count is None else min(start + count, len(records))
    selected = records[start:end]
    stem = re.sub(r'[^\w.-]+', '_', Path(motion['path']).stem)[:100]
    # UE 5.3's bundled Python 3.9 can return a relative Path.resolve() result
    # for a destination that does not exist yet. Anchor before resolving.
    output = Path(os.path.abspath(str(output_root))) / stem / avatar / job_id
    if not output.is_absolute():
        raise ValueError('Output directory must resolve to an absolute path')
    output.mkdir(parents=True, exist_ok=False)
    stage = RUNTIME / 'staged'
    stage.mkdir(parents=True, exist_ok=True)
    subject = 'MH' + job_id.replace('_', '')
    staged_csv = stage / (subject + '.csv')
    with staged_csv.open('x', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(['Timecode', 'BlendShapeCount'] + CHANNELS)
        for record in selected:
            writer.writerow([timecode(record['row']), 61] + record['values'])
    mapping = [{'file': f'{r["row"]:06d}.png', 'csv_row_index': r['row'],
                'capture_frame': 2 * r['row'] + 1,
                'csv_line_number': r['row'] + 2, 'idx': r['idx'],
                'source_timecode': r['timecode']} for r in selected]
    atomic_json(output / 'frame_mapping.json', mapping)
    job = {'schema': 1, 'id': job_id, 'state': 'queued', 'created_at': time.time(),
           'project': settings['project'], 'avatar': avatar, 'preset': preset,
           'source_csv': motion['path'], 'source_sha256': motion['sha256'],
           'source_rows': len(records), 'start_row': start, 'end_row_exclusive': end,
           'expected_images': len(selected), 'staged_csv': str(staged_csv),
           'subject': subject, 'output': str(output), 'fps': 1,
           'render_fps': 2, 'output_frame_step': 2,
           'resolution': settings['resolution']}
    atomic_json(output / 'job.json', job)
    return job


def image_progress(job):
    """Count output images by CSV row, including MRQ's temporary odd frames.

    Read staging first and final files second, using a set to avoid counting an
    image twice while finalization moves it between the two locations.
    """
    output = Path(job['output'])
    first, end = job['start_row'], job['end_row_exclusive']
    written = set()
    for path in (output / '.capture').glob('*.png'):
        if path.stem.isdecimal():
            frame = int(path.stem)
            row = (frame - 1) // 2
            if frame % 2 == 1 and first <= row < end:
                written.add(row)
    for path in output.glob('*.png'):
        if path.stem.isdecimal() and first <= int(path.stem) < end:
            written.add(int(path.stem))
    expected = job['expected_images']
    return {'images_written': len(written), 'expected_images': expected,
            'image_progress_percent': round(100 * len(written) / expected, 2) if expected else 0.0}


def finalize_capture(job):
    """Move each settled sample from MRQ's 2 fps clock to its CSV row filename."""
    folder = Path(job['output'])
    raw = folder / '.capture'
    expected = {f'{2*i+1:06d}.png' for i in range(job['start_row'], job['end_row_exclusive'])}
    actual = {p.name for p in raw.glob('*.png')}
    if actual != expected:
        raise ValueError(f'Capture frames mismatch: missing={sorted(expected-actual)[:10]}, '
                         f'extra={sorted(actual-expected)[:10]}')
    for i in range(job['start_row'], job['end_row_exclusive']):
        target = folder / f'{i:06d}.png'
        if target.exists():
            raise FileExistsError(target)
    for i in range(job['start_row'], job['end_row_exclusive']):
        (raw / f'{2*i+1:06d}.png').rename(folder / f'{i:06d}.png')


def validate_images(job):
    folder = Path(job['output'])
    expected = {f'{i:06d}.png' for i in range(job['start_row'], job['end_row_exclusive'])}
    actual = {p.name for p in folder.glob('*.png')}
    if actual != expected:
        raise ValueError(f'Image set mismatch: missing={sorted(expected-actual)[:10]}, '
                         f'extra={sorted(actual-expected)[:10]}')
    for name in sorted(expected):
        with (folder / name).open('rb') as handle:
            header = handle.read(24)
        if len(header) != 24 or header[:8] != b'\x89PNG\r\n\x1a\n':
            raise ValueError(f'Invalid PNG: {name}')
        if list(struct.unpack('>II', header[16:24])) != job['resolution']:
            raise ValueError(f'Unexpected resolution: {name}')
    return len(actual)
