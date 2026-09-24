"""Put the repo root on sys.path so tests can import the flat rig modules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
