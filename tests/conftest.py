"""Point Otto's config at the repo (not the real ~/Library dir) during tests, so the suite
reads the packaged config.yaml and never pollutes the user's data dir."""

import os
from pathlib import Path

# Must be set before `otto.config` is imported.
os.environ.setdefault("OTTO_HOME", str(Path(__file__).resolve().parent.parent))
