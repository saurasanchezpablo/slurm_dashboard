"""Shared fixtures.

The dashboard reads its config and history from $HOME / $XDG_CONFIG_HOME at
import time, so both are redirected at a temporary directory *before* the
module is imported. Tests must never touch a developer's real files.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "slurm_dashboard.py"

_SANDBOX = Path(tempfile.mkdtemp(prefix="sqdash-tests-"))
os.environ["HOME"] = str(_SANDBOX)
os.environ["XDG_CONFIG_HOME"] = str(_SANDBOX / ".config")
os.environ.setdefault("USER", "testuser")


def _load():
    spec = importlib.util.spec_from_file_location("slurm_dashboard", SRC)
    module = importlib.util.module_from_spec(spec)
    sys.modules["slurm_dashboard"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def sd():
    return _load()


@pytest.fixture
def isolated(sd, tmp_path, monkeypatch):
    """Point every on-disk artefact at a per-test directory."""
    monkeypatch.setattr(sd, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(sd, "EVENT_LOG_FILE", tmp_path / "events.log")
    monkeypatch.setattr(sd, "TEMPLATE_FILE", tmp_path / "templates.json")
    monkeypatch.setattr(sd, "CONFIG_FILE", tmp_path / "config.ini")
    monkeypatch.setattr(sd, "MY_USER", "testuser")
    return tmp_path


@pytest.fixture
def fake_slurm(sd, monkeypatch):
    """Route every Slurm invocation to canned output.

    Usage:  fake_slurm.set("squeue", "...")  then call the parser.
    Unregistered commands return "" rather than reaching a real binary.
    """
    class Fake:
        def __init__(self):
            self.responses: dict[str, str] = {}
            self.calls: list[list[str]] = []

        def set(self, binary, output):
            self.responses[binary] = output

        def __call__(self, cmd, timeout=10):
            self.calls.append(list(cmd))
            return (self.responses.get(cmd[0], ""), "")

    fake = Fake()
    monkeypatch.setattr(sd, "run", fake)
    monkeypatch.setattr(sd, "run_out", lambda cmd: fake(cmd)[0])
    return fake
