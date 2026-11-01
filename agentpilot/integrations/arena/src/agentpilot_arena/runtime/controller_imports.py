"""Trusted package roots required by Harbor's controller subprocess."""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path

_CONTROLLER_PACKAGES = ("agentpilot_arena", "agentpilot", "rsi_arena")


def trusted_controller_import_roots() -> tuple[Path, ...]:
    """Resolve installed or editable package roots without layout assumptions."""

    roots: list[Path] = []
    for package_name in _CONTROLLER_PACKAGES:
        spec = find_spec(package_name)
        if spec is None or spec.origin is None:
            raise RuntimeError(
                f"Harbor controller dependency {package_name!r} is not installed"
            )
        package_dir = Path(spec.origin).resolve(strict=True).parent
        root = package_dir.parent
        if not root.is_dir():
            raise RuntimeError(
                f"Harbor controller dependency {package_name!r} has no import root"
            )
        if root not in roots:
            roots.append(root)
    return tuple(roots)


__all__ = ["trusted_controller_import_roots"]
