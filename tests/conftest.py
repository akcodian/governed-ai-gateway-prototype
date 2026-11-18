from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import Recorder, SpanIndex, Stack  # noqa: E402

RECORDER = Recorder()


@pytest.fixture(scope="session")
def stack() -> Stack:
    s = Stack()
    try:
        s.health()
    except Exception as exc:  # noqa: BLE001
        pytest.exit(f"stack is not running (make up): {exc}", returncode=2)
    return s


@pytest.fixture(scope="session")
def spans() -> SpanIndex:
    return SpanIndex()


@pytest.fixture
def clean(stack):
    stack.reset()
    yield stack
    stack.reset()


@pytest.fixture
def scenario():
    def _open(name: str, expected: str):
        return RECORDER.open("scenarios", name, expected)
    return _open


@pytest.fixture
def supplementary():
    def _open(name: str, expected: str):
        return RECORDER.open("supplementary", name, expected)
    return _open


def pytest_sessionfinish(session, exitstatus):
    RECORDER.write()
