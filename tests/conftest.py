"""Make `story_automator`/`claudomater` importable from `src/` without installing."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


@pytest.fixture
def omater_on_path(tmp_path_factory, monkeypatch):
    """`omater init --verify` checks that the hook's `omater` command is on
    PATH. In an uninstalled test environment, satisfy it with a stub."""
    if shutil.which("omater"):
        return
    fakebin = tmp_path_factory.mktemp("fakebin")
    exe = fakebin / "omater"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}")
