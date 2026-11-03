"""Run inside Unreal to create missing template snapshots. Existing ones are kept."""
import sys
from pathlib import Path
root = str(Path(__file__).resolve().parents[1])
if root not in sys.path:
    sys.path.insert(0, root)
import unreal
from image_render.render_common import config
from image_render.ue_batch import duplicate_map, save_objects

for name, preset in config()['presets'].items():
    if unreal.EditorAssetLibrary.does_asset_exist(preset['map']):
        unreal.log('Template already exists: ' + preset['map'])
    else:
        world = duplicate_map(preset['source_map'], preset['map'])
        save_objects([world])
        unreal.log('Created template: ' + preset['map'])
