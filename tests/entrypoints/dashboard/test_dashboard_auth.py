"""Tests for dashboard API authentication off loopback."""

from __future__ import annotations

from pathlib import Path

import pytest

from openspace.entrypoints.dashboard.server import (
    DASHBOARD_TOKEN_ENV,
    create_app,
)


@pytest.fixture()
def skill_db(tmp_path: Path) -> Path:
    return tmp_path / "openspace.db"


def test_loopback_api_open_without_token(skill_db: Path, monkeypatch) -> None:
    monkeypatch.delenv(DASHBOARD_TOKEN_ENV, raising=False)
    app = create_app(db_path=skill_db, bind_host="127.0.0.1")
    client = app.test_client()

    response = client.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"


def test_non_loopback_requires_token_env(skill_db: Path, monkeypatch) -> None:
    monkeypatch.delenv(DASHBOARD_TOKEN_ENV, raising=False)
    app = create_app(db_path=skill_db, bind_host="0.0.0.0")
    client = app.test_client()

    response = client.get("/api/v1/health")
    assert response.status_code == 403
    assert DASHBOARD_TOKEN_ENV in response.get_json()["message"]


def test_non_loopback_rejects_missing_bearer(skill_db: Path, monkeypatch) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_ENV, "super-secret")
    app = create_app(db_path=skill_db, bind_host="0.0.0.0")
    client = app.test_client()

    response = client.get("/api/v1/skills")
    assert response.status_code == 401


def test_non_loopback_accepts_bearer_token(skill_db: Path, monkeypatch) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_ENV, "super-secret")
    app = create_app(db_path=skill_db, bind_host="0.0.0.0")
    client = app.test_client()

    response = client.get(
        "/api/v1/health",
        headers={"Authorization": "Bearer super-secret"},
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_non_loopback_accepts_custom_header(skill_db: Path, monkeypatch) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_ENV, "super-secret")
    app = create_app(db_path=skill_db, bind_host="0.0.0.0")
    client = app.test_client()

    response = client.get(
        "/api/v1/overview",
        headers={"X-OpenSpace-Dashboard-Token": "super-secret"},
    )
    assert response.status_code == 200


def test_static_root_remains_reachable_without_token(
    skill_db: Path, monkeypatch
) -> None:
    monkeypatch.delenv(DASHBOARD_TOKEN_ENV, raising=False)
    app = create_app(db_path=skill_db, bind_host="0.0.0.0")
    client = app.test_client()

    response = client.get("/")
    assert response.status_code == 200
