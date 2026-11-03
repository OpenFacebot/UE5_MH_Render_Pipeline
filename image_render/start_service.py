"""Run inside Unreal: py "<repository>/image_render/start_service.py"."""
import sys
from pathlib import Path

root = str(Path(__file__).resolve().parents[1])
if root not in sys.path:
    sys.path.insert(0, root)
from image_render import ue_batch
ue_batch.start()
