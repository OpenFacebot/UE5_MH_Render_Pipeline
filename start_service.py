"""Run once inside Unreal: py "<absolute path>/start_service.py"."""
import sys
from pathlib import Path

root = str(Path(__file__).resolve().parent)
if root not in sys.path:
    sys.path.insert(0, root)
import ue_batch
ue_batch.start()
