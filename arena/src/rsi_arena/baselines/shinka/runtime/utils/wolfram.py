"""Cross-platform helpers for invoking ``wolframscript`` from subprocess."""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

WOLFRAMSCRIPT_BIN_ENV = "WOLFRAMSCRIPT_BIN"
DEFAULT_WOLFRAMSCRIPT_BIN = "wolframscript"


def wolframscript_bin() -> str:
    """The configured ``wolframscript`` binary name or path.

    Read from ``WOLFRAMSCRIPT_BIN`` at call time so the environment can be set
    after import, matching how the rest of the codebase reads provider config.
    """
    return os.environ.get(WOLFRAMSCRIPT_BIN_ENV, DEFAULT_WOLFRAMSCRIPT_BIN)


def resolve_wolframscript_bin() -> str:
    """Absolute path to ``wolframscript``, or the raw configured value if not found.

    Searches PATH (and PATHEXT on Windows). On WSL we also try the ``.exe``
    fallback because Wolfram Engine is typically installed on the Windows
    host and only the Win32 binary is on the cross-distro PATH; WSL interop
    can execute it directly. The caller is expected to error with an
    actionable message when the returned value cannot actually be executed.
    """
    configured = wolframscript_bin()
    resolved = shutil.which(configured)
    if (
        resolved is None
        and not configured.lower().endswith(".exe")
        and (os.name == "nt" or is_wsl())
    ):
        resolved = shutil.which(configured + ".exe")
    return resolved or configured


def is_wolframscript_available() -> bool:
    """True if ``wolframscript`` can be located or its absolute path exists."""
    bin_path = resolve_wolframscript_bin()
    return shutil.which(bin_path) is not None or Path(bin_path).is_file()


def check_wolframscript_available() -> None:
    """Raise ``ValueError`` if ``wolframscript`` cannot be located.

    Mirrors ``shinka.llm.providers.headless.check_headless_available`` so the
    async runner's pre-run model-env validation can fail fast with an
    actionable message instead of surfacing the error mid-run.
    """
    if not is_wolframscript_available():
        raise ValueError(
            f"`{wolframscript_bin()}` not found on PATH. Install Wolfram Engine "
            "or Mathematica, or set WOLFRAMSCRIPT_BIN to the absolute path of "
            "the binary."
        )


def is_wsl() -> bool:
    """True when the current process is running inside a WSL distro."""
    if "WSL_DISTRO_NAME" in os.environ:
        return True
    try:
        return Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()
    except OSError:
        return False


def is_shell_script(path: str) -> bool:
    """True if ``path`` begins with a ``#!`` shebang."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def build_wolframscript_argv(args: list[str]) -> list[str]:
    """Build an argv that invokes wolframscript with the given trailing args.

    On WSL where ``wolframscript`` is a shell-script wrapper around
    ``wolframscript.exe``, route through ``bash -c`` because direct execve
    of the wrapper hangs on the WSL/Win32 interop path. Everywhere else,
    invoke the binary directly.
    """
    bin_path = resolve_wolframscript_bin()
    parts = [bin_path, *args]
    if is_wsl() and is_shell_script(bin_path):
        return ["bash", "-c", " ".join(shlex.quote(p) for p in parts)]
    return parts


def escape_wolfram_string(s: str) -> str:
    """Escape ``s`` so it can be safely embedded in a Wolfram string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
