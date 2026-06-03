from pathlib import Path


def test_litellm_pin_window_matches_safe_versions() -> None:
    pyproject = Path("pyproject.toml").read_text()
    requirements = Path("requirements.txt").read_text()
    expected = "litellm>=1.70.0,!=1.82.7,!=1.82.8"

    assert expected in pyproject
    assert expected in requirements
    assert "litellm>=1.70.0,<1.82.7" not in pyproject
    assert "litellm>=1.70.0,<1.82.7" not in requirements
