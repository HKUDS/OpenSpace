import importlib.util
from pathlib import Path


def _load_module():
    module_path = Path(__file__).resolve().parents[2] / "openspace" / "config" / "utils.py"
    spec = importlib.util.spec_from_file_location("openspace_config_utils_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_utils = _load_module()
get_config_value = _utils.get_config_value
load_json_file = _utils.load_json_file
save_json_file = _utils.save_json_file


def test_pytest_scaffold_uses_tests_directory(pytestconfig):
    assert pytestconfig.getini("testpaths") == ["tests"]


def test_get_config_value_supports_dict_and_object():
    config_dict = {"value": 42}

    class ConfigObject:
        value = 42

    assert get_config_value(config_dict, "value") == 42
    assert get_config_value(ConfigObject(), "value") == 42
    assert get_config_value(config_dict, "missing", "fallback") == "fallback"


def test_save_and_load_json_round_trip(tmp_path: Path):
    payload = {"name": "openspace", "nested": {"enabled": True}}
    target = tmp_path / "nested" / "config.json"

    save_json_file(payload, target)

    assert target.exists()
    assert load_json_file(target) == payload
