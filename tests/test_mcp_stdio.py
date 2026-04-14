from __future__ import annotations

import io
from pathlib import Path

from openspace import mcp_stdio


class _FakeStream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _reset_capture() -> None:
    handle = mcp_stdio._STDERR_CAPTURE_HANDLE
    if handle is not None:
        handle.close()
    mcp_stdio._STDERR_CAPTURE_HANDLE = None


def test_redirects_stderr_for_non_interactive_sessions(monkeypatch, tmp_path: Path) -> None:
    _reset_capture()
    monkeypatch.setattr("sys.stderr", _FakeStream(tty=False))

    handle = mcp_stdio.maybe_redirect_stderr_to_file(tmp_path, "stderr.log")

    assert handle is not None
    assert Path(handle.name) == tmp_path / "stderr.log"
    assert Path(handle.name).exists()
    assert handle is mcp_stdio._STDERR_CAPTURE_HANDLE

    _reset_capture()


def test_keeps_stderr_for_interactive_sessions(monkeypatch, tmp_path: Path) -> None:
    _reset_capture()
    original = _FakeStream(tty=True)
    monkeypatch.setattr("sys.stderr", original)

    handle = mcp_stdio.maybe_redirect_stderr_to_file(tmp_path, "stderr.log")

    assert handle is None
    assert mcp_stdio._STDERR_CAPTURE_HANDLE is None
    assert original is not None
