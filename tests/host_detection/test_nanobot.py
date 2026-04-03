import importlib.util
from pathlib import Path


def _load_module():
    module_path = Path(__file__).resolve().parents[2] / "openspace" / "host_detection" / "nanobot.py"
    spec = importlib.util.spec_from_file_location("openspace_nanobot_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_nanobot = _load_module()
match_provider = _nanobot.match_provider


def test_match_provider_prefers_forced_provider():
    providers = {
        "openai": {"apiKey": "oa-key", "apiBase": "https://openai.example/v1"},
        "minimax": {"apiKey": "mini-key", "apiBase": "https://minimax.example/v1"},
    }

    result = match_provider(providers, model="anything", forced_provider="minimax")

    assert result == {
        "api_key": "mini-key",
        "api_base": "https://minimax.example/v1",
    }


def test_read_nanobot_mcp_env_returns_openspace_env(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "tools": {
            "mcpServers": {
              "openspace": {
                "env": {
                  "OPENSPACE_MODEL": "openrouter/anthropic/claude"
                }
              }
            }
          }
        }
        """.strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(_nanobot, "NANOBOT_CONFIG_PATH", config_path)

    assert _nanobot.read_nanobot_mcp_env() == {"OPENSPACE_MODEL": "openrouter/anthropic/claude"}


def test_try_read_nanobot_config_extracts_model_and_provider(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "providers": {
            "minimax": {
              "apiKey": "mini-key",
              "apiBase": "https://minimax.example/v1"
            }
          },
          "agents": {
            "defaults": {
              "model": "minimax/text-01",
              "provider": "minimax"
            }
          }
        }
        """.strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(_nanobot, "NANOBOT_CONFIG_PATH", config_path)

    result = _nanobot.try_read_nanobot_config("")

    assert result == {
        "api_key": "mini-key",
        "api_base": "https://minimax.example/v1",
        "_model": "minimax/text-01",
        "_forced_provider": "minimax",
    }
