import os
import unittest
from unittest.mock import patch

from openspace.host_detection.resolver import build_llm_kwargs, build_grounding_config_path


class ResolverWhitespaceEnvTests(unittest.TestCase):
    @patch("openspace.host_detection.nanobot.try_read_nanobot_config")
    @patch("openspace.host_detection.openclaw.try_read_openclaw_config")
    def test_whitespace_explicit_llm_env_does_not_disable_host_fallback(self, mock_openclaw, mock_nanobot):
        mock_nanobot.return_value = {"api_key": "host-key", "api_base": "https://host.example/v1"}
        mock_openclaw.return_value = None

        with patch.dict(
            os.environ,
            {
                "OPENSPACE_LLM_API_KEY": "   ",
                "OPENSPACE_LLM_API_BASE": "\t",
                "OPENROUTER_API_KEY": "",
            },
            clear=False,
        ):
            model, kwargs = build_llm_kwargs("openrouter/anthropic/claude-sonnet-4.5")

        self.assertEqual(model, "openrouter/anthropic/claude-sonnet-4.5")
        self.assertEqual(kwargs.get("api_key"), "host-key")
        self.assertEqual(kwargs.get("api_base"), "https://host.example/v1")

    def test_whitespace_config_path_returns_none(self):
        with patch.dict(os.environ, {"OPENSPACE_CONFIG_PATH": "   "}, clear=False):
            self.assertIsNone(build_grounding_config_path())


if __name__ == "__main__":
    unittest.main()
