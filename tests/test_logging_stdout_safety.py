from __future__ import annotations

import io
import logging
from pathlib import Path

from openspace.utils.logging import Logger


def test_log_file_enable_announcement_avoids_stdout(monkeypatch, tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)

    Logger.reset_configuration()
    Logger.configure(
        level=logging.INFO,
        log_to_console=False,
        log_to_file=str(tmp_path / "unit.log"),
        attach_to_root=True,
    )

    assert stdout.getvalue() == ""
    assert "Log file enabled:" in stderr.getvalue()

    Logger.reset_configuration()


def test_console_logging_avoids_stdout(monkeypatch, tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)

    Logger.reset_configuration()
    Logger.configure(
        level=logging.INFO,
        log_to_console=True,
        log_to_file=str(tmp_path / "unit.log"),
        attach_to_root=True,
    )
    Logger.get_logger("openspace.test").info("console log should stay off stdout")

    assert stdout.getvalue() == ""
    assert "console log should stay off stdout" in stderr.getvalue()

    Logger.reset_configuration()
