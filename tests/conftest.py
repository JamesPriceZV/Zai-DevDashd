"""Load the outside config before any test imports the aggregator."""

from __future__ import annotations

import os
import json
from pathlib import Path
import tempfile

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="zai-devdashd-test-")
_root = Path(_TEST_ROOT.name)
_config = _root / "config.toml"
_config.write_text('[state]\ndir = ' + json.dumps(str(_root / "state")) + '\n')
_config.chmod(0o600)
os.environ["ZAI_DEVDASHD_CONFIG"] = str(_config)

from zai_devdashd import config as site

site.load(Path(os.environ["ZAI_DEVDASHD_CONFIG"]))


def pytest_sessionfinish(session, exitstatus):
    _TEST_ROOT.cleanup()
