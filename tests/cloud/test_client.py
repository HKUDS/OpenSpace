import importlib.util
import io
import zipfile
from pathlib import Path

import pytest


def _load_module():
    module_path = Path(__file__).resolve().parents[2] / "openspace" / "cloud" / "client.py"
    spec = importlib.util.spec_from_file_location("openspace_cloud_client_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_client = _load_module()
CloudError = _client.CloudError
OpenSpaceClient = _client.OpenSpaceClient


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def test_extract_zip_skips_path_traversal_entries(tmp_path: Path):
    zip_data = _zip_bytes(
        {
            "SKILL.md": "name: demo",
            "../escape.txt": "nope",
            "/absolute.txt": "nope",
            "nested/file.txt": "ok",
        }
    )

    extracted = OpenSpaceClient._extract_zip(zip_data, tmp_path)

    assert extracted == ["SKILL.md", "nested/file.txt"]
    assert (tmp_path / "SKILL.md").read_text(encoding="utf-8") == "name: demo"
    assert (tmp_path / "nested" / "file.txt").read_text(encoding="utf-8") == "ok"


def test_validate_origin_parents_enforces_fixed_origin():
    with pytest.raises(CloudError, match="exactly 1 parent_skill_id"):
        OpenSpaceClient._validate_origin_parents("fixed", [])

    OpenSpaceClient._validate_origin_parents("fixed", ["parent-1"])


def test_unified_diff_returns_none_when_snapshots_match():
    assert OpenSpaceClient._unified_diff({"a.txt": "same\n"}, {"a.txt": "same\n"}) is None
