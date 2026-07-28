"""Tests for writable OpenSpace data-home resolution."""

from __future__ import annotations

from pathlib import Path

import openspace.config.constants as constants


def test_is_source_checkout_true_for_repo_layout(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="openspace"\n', encoding="utf-8")
    pkg = tmp_path / "openspace"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("__version__ = '2.0.0'\n", encoding="utf-8")
    assert constants.is_source_checkout(tmp_path) is True


def test_is_source_checkout_false_for_site_packages_layout(tmp_path: Path) -> None:
    pkg = tmp_path / "openspace"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("__version__ = '2.0.0'\n", encoding="utf-8")
    assert constants.is_source_checkout(tmp_path) is False


def test_get_data_home_respects_openspace_home(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "custom-home"
    monkeypatch.setenv("OPENSPACE_HOME", str(home))
    monkeypatch.delenv("OPENSPACE_DATA_DIR", raising=False)

    resolved = constants.get_data_home(create=True)

    assert resolved == home.resolve()
    assert home.is_dir()


def test_get_data_home_respects_openspace_data_dir_alias(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "alias-home"
    monkeypatch.delenv("OPENSPACE_HOME", raising=False)
    monkeypatch.setenv("OPENSPACE_DATA_DIR", str(home))

    resolved = constants.get_data_home(create=True)

    assert resolved == home.resolve()


def test_get_data_home_uses_user_home_when_not_source_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("OPENSPACE_HOME", raising=False)
    monkeypatch.delenv("OPENSPACE_DATA_DIR", raising=False)
    monkeypatch.setattr(constants, "PROJECT_ROOT", tmp_path / "site-packages")
    monkeypatch.setattr(constants, "is_source_checkout", lambda root=None: False)

    resolved = constants.get_data_home(create=True)

    assert resolved == (fake_home / ".openspace").resolve()
    assert resolved.is_dir()


def test_get_data_home_uses_repo_dot_openspace_in_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="openspace"\n', encoding="utf-8")
    pkg = tmp_path / "openspace"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.delenv("OPENSPACE_HOME", raising=False)
    monkeypatch.delenv("OPENSPACE_DATA_DIR", raising=False)
    monkeypatch.setattr(constants, "PROJECT_ROOT", tmp_path)

    resolved = constants.get_data_home(create=True)

    assert resolved == (tmp_path / ".openspace").resolve()


def test_get_default_db_path_and_cache_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSPACE_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("OPENSPACE_DATA_DIR", raising=False)

    db_path = constants.get_default_db_path(create=True)
    cache = constants.get_cache_dir("embedding_cache", create=True)
    logs = constants.get_log_dir(create=True)

    assert db_path == (tmp_path / "data" / "openspace.db").resolve()
    assert cache == (tmp_path / "data" / "embedding_cache").resolve()
    assert logs == (tmp_path / "data" / "logs").resolve()
    assert cache.is_dir()
    assert logs.is_dir()


def test_skill_store_default_path_uses_data_home(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSPACE_HOME", str(tmp_path / "runtime"))
    monkeypatch.delenv("OPENSPACE_DATA_DIR", raising=False)

    from openspace.skill_engine.store import SkillStore

    store = SkillStore()
    try:
        expected = (tmp_path / "runtime" / "openspace.db").resolve()
        assert store.db_path.resolve() == expected
        assert expected.is_file()
    finally:
        store.close()


def test_package_version_matches_pyproject() -> None:
    import openspace
    from pathlib import Path
    import re

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None
    assert openspace.__version__ == match.group(1)


def test_get_workflow_roots_include_data_home_logs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSPACE_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("OPENSPACE_DATA_DIR", raising=False)
    monkeypatch.setattr(constants, "is_source_checkout", lambda root=None: False)

    roots = constants.get_workflow_roots()
    data_home = (tmp_path / "data").resolve()
    assert data_home / "logs" / "recordings" in [p.resolve() for p in roots]
    assert data_home / "logs" / "trajectories" in [p.resolve() for p in roots]
    assert not any("benchmarks" in str(p) for p in roots)


def test_get_workflow_roots_include_repo_paths_in_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="openspace"\n', encoding="utf-8")
    pkg = tmp_path / "openspace"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENSPACE_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("OPENSPACE_DATA_DIR", raising=False)
    monkeypatch.setattr(constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(constants, "is_source_checkout", lambda root=None: True)

    roots = [str(p) for p in constants.get_workflow_roots()]
    assert any(str(tmp_path / "benchmarks" / "gdpval" / "results") == r for r in roots)
    assert any("recordings" in r for r in roots)
