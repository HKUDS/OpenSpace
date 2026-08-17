from __future__ import annotations

from unittest.mock import Mock

import pytest

from openspace.entrypoints.cli.text_loop import interactive_mode


@pytest.mark.asyncio
async def test_interactive_mode_exits_after_stdin_eof(monkeypatch, capsys) -> None:
    calls = 0

    def closed_stdin(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise EOFError

    monkeypatch.setattr("builtins.input", closed_stdin)
    openspace = Mock()
    ui_manager = Mock()

    completed = await interactive_mode(openspace, ui_manager)

    assert completed is False
    assert calls == 1
    openspace.execute.assert_not_called()
    assert "Input stream closed; exiting interactive mode" in capsys.readouterr().err
