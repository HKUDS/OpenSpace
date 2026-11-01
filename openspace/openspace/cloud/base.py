"""Shared helpers for strict OpenSpace cloud API URL handling."""

from __future__ import annotations

from openspace.cloud.config import normalize_cloud_base_url


def cloud_api_url(base_url: str, version: str, path: str) -> str:
    """Build a versioned API URL from a strict service root URL."""

    if version not in {"v1", "v2"}:
        raise ValueError("version must be 'v1' or 'v2'")
    base = normalize_cloud_base_url(base_url)
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{base}/api/{version}{normalized_path}"
