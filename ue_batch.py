"""Unreal Editor side of the MetaHuman CSV renderer (UE 5.3)."""
import json
import os
import time
import traceback
from pathlib import Path

import unreal
from render_common import ROOT, RUNTIME, atomic_json, config, read_json, validate_images, finalize_capture


def world_asset(path):
    obj_path = path + '.' + path.rsplit('/', 1)[-1] if '.' not in path else path
    obj = unreal.find_object(None, obj_path) or unreal.load_asset(path)
    if not isinstance(obj, unreal.World):
        raise ValueError('Cannot load map: ' + path)
    return obj


def actors_in(world):
    return unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)


def unique_actor(actors, label):
    matches = [a for a in actors if a.get_actor_label() == label]
    if len(matches) != 1:
        raise ValueError(f'Expected one actor labelled {label}, got {len(matches)}')
    return matches[0]


def save_objects(objects):
    packages = [obj.get_outermost() for obj in objects]
    if not unreal.EditorLoadingAndSavingUtils.save_packages(packages, False):
        raise RuntimeError('Could not save generated assets')


def duplicate_map(source, destination):
    if unreal.EditorAssetLibrary.does_asset_exist(destination):
        raise FileExistsError(destination)
    folder, name = destination.rsplit('/', 1)
    world = unreal.AssetToolsHelpers.get_asset_tools().duplicate_asset(name, folder, world_asset(source))
    if not isinstance(world, unreal.World):
        raise RuntimeError('Map duplication failed: ' + destination)
    return world


def build_job(job):
    cfg = config()
    preset = cfg['presets'][job['preset']]
    folder = cfg['asset_root'] + '/Jobs/J' + job['id']
    world = duplicate_map(preset['map'], folder + '/RenderMap')
    actors = actors_in(world)
    actor = unique_actor(actors, preset['actor'])
    camera = unique_actor(actors, preset['camera'])
    if not isinstance(camera, unreal.CineCameraActor):
        raise TypeError('Template camera is not a CineCameraActor')
    actor.set_editor_property('ARKitFaceSubj', unreal.LiveLinkSubjectName(job['subject']))
    actor.set_editor_property('UseARKitFace', True)
    actor.set_editor_property('UseLiveLinkBody', False)
    component = camera.get_cine_camera_component()
    pp = component.get_editor_property('post_process_settings')
    pp.set_editor_property('override_motion_blur_amount', True)
    pp.set_editor_property('motion_blur_amount', 0.0)
    component.set_editor_property('post_process_settings', pp)

    task = unreal.AssetImportTask()
    task.filename = job['staged_csv']
    task.destination_path = folder
    task.automated = True
    task.save = False
    task.factory = unreal.LiveLinkFaceImporterFactory()
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    imported = task.get_objects()
    if len(imported) != 1 or not isinstance(imported[0], unreal.LevelSequence):
        raise RuntimeError('CSV importer did not return one LevelSequence')
    sequence = imported[0]
    # Live Link publishes during sequence evaluation and its animation consumer
    # reads on the next game tick. Evaluate each held row twice at 2 fps, and
    # render only the second (odd) frame. Skipped frames still tick the game.
    first, end = 2 * job['start_row'] + 1, 2 * job['end_row_exclusive']
    warm_start = first - cfg['engine_warm_up_frames'] - 2
    sequence.set_display_rate(unreal.FrameRate(2, 1))
    sequence.set_playback_start(first)
    sequence.set_playback_end(end)
    channels = 0
    for track in sequence.get_tracks():
        if isinstance(track, unreal.MovieSceneLiveLinkTrack):
            for section in track.get_sections():
                section.set_range(warm_start, end + 1)
                for channel in section.get_all_channels():
                    keys = channel.get_keys()
                    if len(keys) != job['expected_images']:
                        raise ValueError(f'Imported channel has {len(keys)} keys; expected {job["expected_images"]}')
                    for key in keys:
                        key.set_interpolation_mode(unreal.RichCurveInterpMode.RCIM_CONSTANT)
                    channels += 1
    if channels != 61:
        raise ValueError(f'Expected 61 Live Link curves, got {channels}')
    sequence.add_possessable(actor)
    binding = sequence.add_possessable(camera)
    transform = binding.add_track(unreal.MovieScene3DTransformTrack).add_section()
    transform.set_range(warm_start, end + 1)
    loc, rot, scale = camera.get_actor_location(), camera.get_actor_rotation(), camera.get_actor_scale3d()
    for channel, value in zip(transform.get_all_channels(),
                              [loc.x, loc.y, loc.z, rot.roll, rot.pitch, rot.yaw, scale.x, scale.y, scale.z]):
        channel.set_default(value)
    cut = sequence.add_track(unreal.MovieSceneCameraCutTrack).add_section()
    cut.set_range(warm_start, end + 1)
    cut.set_camera_binding_id(sequence.get_binding_id(binding))
    save_objects([world, sequence])
    job.update({'map_asset': world.get_path_name(), 'sequence_asset': sequence.get_path_name(),
                'actor_class': actor.get_class().get_path_name(), 'live_link_channels': channels,
                'camera': {'location': [loc.x, loc.y, loc.z], 'rotation': [rot.pitch, rot.yaw, rot.roll],
                           'focal_length': component.current_focal_length,
                           'aperture': component.current_aperture}})

    queue = unreal.MoviePipelineQueue()
    render_job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    render_job.job_name = job['id']
    render_job.map = unreal.SoftObjectPath(world.get_path_name())
    render_job.sequence = unreal.SoftObjectPath(sequence.get_path_name())
    settings = render_job.get_configuration()
    output = settings.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    output.output_directory = unreal.DirectoryPath(str(Path(job['output']) / '.capture'))
    output.file_name_format = '{frame_number}'
    output.zero_pad_frame_numbers = 6
    output.output_resolution = unreal.IntPoint(*job['resolution'])
    output.use_custom_frame_rate = True
    output.output_frame_rate = unreal.FrameRate(2, 1)
    output.output_frame_step = 2
    output.use_custom_playback_range = True
    output.custom_start_frame = first
    output.custom_end_frame = end
    output.override_existing_output = False
    settings.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)
    settings.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
    aa = settings.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
    aa.temporal_sample_count = 1
    aa.spatial_sample_count = cfg['spatial_samples']
    aa.override_anti_aliasing = True
    aa.anti_aliasing_method = unreal.AntiAliasingMethod.AAM_NONE
    aa.engine_warm_up_count = cfg['engine_warm_up_frames']
    aa.render_warm_up_count = cfg['render_warm_up_frames']
    aa.render_warm_up_frames = True
    aa.use_camera_cut_for_warm_up = False
    camera_setting = settings.find_or_add_setting_by_class(unreal.MoviePipelineCameraSetting)
    camera_setting.shutter_timing = unreal.MoviePipelineShutterTiming.FRAME_OPEN
    return queue


class RenderService:
    def __init__(self):
        self.active = None
        self.executor = None
        self.queue = None
        self.finished = None
        self.last_poll = 0
        self.last_heartbeat = 0
        self.handle = None
        self.inbox = RUNTIME / 'requests'
        self.inbox.mkdir(parents=True, exist_ok=True)

    def persist(self):
        self.active['updated_at'] = time.time()
        atomic_json(Path(self.active['output']) / 'job.json', self.active)
        atomic_json(RUNTIME / 'status' / (self.active['id'] + '.json'), self.active)

    def on_finished(self, executor, success):
        # Defer filesystem verification and the next PIE session until this one has torn down.
        self.finished = (bool(success), time.monotonic())

    def on_error(self, executor, pipeline, fatal, error_text):
        if self.active is not None:
            self.active.setdefault('render_errors', []).append({'fatal': bool(fatal), 'message': str(error_text)})
            self.persist()

    def finish(self):
        success, _ = self.finished
        try:
            if self.active.get('cancel_requested'):
                self.active['state'] = 'cancelled'
                self.active['finished_at'] = time.time()
                self.persist()
                self.active = self.executor = self.queue = self.finished = None
                return
            if not success or any(e['fatal'] for e in self.active.get('render_errors', [])):
                raise RuntimeError('Movie Render Queue reported a failure')
            finalize_capture(self.active)
            self.active['verified_images'] = validate_images(self.active)
            self.active['state'] = 'completed'
        except Exception:
            self.active['state'] = 'failed'
            self.active['error'] = traceback.format_exc()
        self.active['finished_at'] = time.time()
        self.persist()
        unreal.log('MH render ' + self.active['state'] + ': ' + self.active['id'])
        self.active = self.executor = self.queue = self.finished = None

    def tick(self, delta):
        now = time.monotonic()
        if now - self.last_heartbeat >= 2:
            atomic_json(RUNTIME / 'heartbeat.json', {'pid': os.getpid(), 'time': time.time(),
                        'project': str(Path(unreal.Paths.get_project_file_path()).resolve()),
                        'active_job': self.active['id'] if self.active else None})
            self.last_heartbeat = now
        if self.finished and now - self.finished[1] >= 1:
            self.finish()
        if self.active and self.executor and self.finished is None and not self.active.get('cancel_requested'):
            marker = RUNTIME / 'cancel' / (self.active['id'] + '.json')
            if marker.exists():
                # UE 5.3 can assert while restoring an uncached shot range if
                # CancelAllJobs is called before the first shot initializes.
                # A written frame proves that initialization has finished.
                if any((Path(self.active['output']) / '.capture').glob('*.png')):
                    self.active['cancel_requested'] = True
                    self.persist()
                    self.executor.cancel_all_jobs()
        if self.active is not None or now - self.last_poll < 0.5:
            return
        self.last_poll = now
        subsystem = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
        if subsystem.is_rendering() or unreal.EditorLevelLibrary.get_game_world():
            return
        for request in sorted(self.inbox.glob('*.json')):
            if (RUNTIME / 'status' / (request.stem + '.json')).exists():
                # A terminal status means this request has already been processed.
                request.rename(request.with_suffix('.duplicate'))
                continue
            try:
                job = read_json(request)
                request.rename(request.with_suffix('.claimed'))
                self.active = job
                if Path(job['project']).resolve() != Path(unreal.Paths.get_project_file_path()).resolve():
                    raise RuntimeError('Request targets a different Unreal project')
                if job['schema'] != 1 or job['preset'] not in config()['presets']:
                    raise ValueError('Invalid job schema or preset')
                if not Path(job['output']).is_absolute():
                    raise ValueError('Job output directory must be absolute')
                if (RUNTIME / 'cancel' / (job['id'] + '.json')).exists():
                    self.active['state'] = 'cancelled'
                    self.persist()
                    self.active = None
                    return
                self.active['state'] = 'preparing'
                self.active['started_at'] = time.time()
                self.persist()
                self.queue = build_job(job)
                self.executor = unreal.MoviePipelinePIEExecutor()
                self.executor.on_executor_finished_delegate.add_callable_unique(self.on_finished)
                self.executor.on_executor_errored_delegate.add_callable_unique(self.on_error)
                self.active['state'] = 'rendering'
                self.persist()
                self.executor.execute(self.queue)
            except Exception:
                if self.active:
                    self.active['state'] = 'failed'
                    self.active['error'] = traceback.format_exc()
                    self.persist()
                    unreal.log_error(self.active['error'])
                else:
                    unreal.log_error(traceback.format_exc())
                    request.rename(request.with_suffix('.invalid'))
                self.active = self.queue = self.executor = self.finished = None
            return


def start():
    if Path(unreal.Paths.get_project_file_path()).resolve() != Path(config()['project']).resolve():
        raise RuntimeError('This renderer is configured for ' + config()['project'])
    old = getattr(unreal, '_mh_render_service', None)
    if old:
        if old.active:
            unreal.log('MH render service is already busy')
            return old
        unreal.unregister_slate_post_tick_callback(old.handle)
    for path in (RUNTIME / 'status').glob('*.json'):
        status = read_json(path)
        if status.get('state') in ('preparing', 'rendering'):
            status['state'] = 'interrupted'
            status['error'] = 'Unreal render service restarted before this job completed. Submit a new job to retry.'
            atomic_json(path, status)
            atomic_json(Path(status['output']) / 'job.json', status)
    service = RenderService()
    service.handle = unreal.register_slate_post_tick_callback(service.tick)
    unreal._mh_render_service = service
    unreal.log('MH render service ready (local file queue; no network port)')
    return service
