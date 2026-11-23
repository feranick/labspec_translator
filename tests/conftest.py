"""Every test runs on the simulator in its own folders (LS6T_HOME, LS6T_OUT under tmp_path)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labspec_translator import sim as S  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LS6T_MODE", "sim")
    monkeypatch.setenv("LS6T_OUT", str(tmp_path / "runs"))
    monkeypatch.setenv("LS6T_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LS6T_PROFILE", raising=False)
    monkeypatch.delenv("LS6T_MAX_MINUTES", raising=False)
    monkeypatch.setenv("LS6T_CONFIRM", "request")
    yield
    S.stop_all()
