"""Unit tests for MiniMax provider support in OpenSpace."""

import os
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from openspace.llm.client import (
    _is_minimax_model,
    _apply_minimax_constraints,
)
from openspace.host_detection.nanobot import match_provider, PROVIDER_REGISTRY
from openspace.host_detection.resolver import build_llm_kwargs


class TestIsMiniMaxModel(unittest.TestCase):
    """Tests for MiniMax model detection."""

    def test_minimax_prefixed_model(self):
        self.assertTrue(_is_minimax_model("minimax/MiniMax-M2.7"))

    def test_minimax_highspeed_model(self):
        self.assertTrue(_is_minimax_model("minimax/MiniMax-M2.7-highspeed"))

    def test_minimax_case_insensitive(self):
        self.assertTrue(_is_minimax_model("MiniMax/MiniMax-M2.7"))

    def test_minimax_in_openai_compat(self):
        self.assertTrue(_is_minimax_model("openai/MiniMax-M2.7"))

    def test_non_minimax_openai_model(self):
        self.assertFalse(_is_minimax_model("openai/gpt-4o"))

    def test_non_minimax_anthropic_model(self):
        self.assertFalse(_is_minimax_model("anthropic/claude-sonnet-4-5"))

    def test_non_minimax_openrouter_model(self):
        self.assertFalse(_is_minimax_model("openrouter/anthropic/claude-sonnet-4.5"))

    def test_empty_string(self):
        self.assertFalse(_is_minimax_model(""))


class TestApplyMiniMaxConstraints(unittest.TestCase):
    """Tests for MiniMax parameter constraints."""

    def test_clamp_temperature_zero(self):
        """Temperature 0 should be clamped to 0.01."""
        kwargs = {"model": "minimax/MiniMax-M2.7", "temperature": 0}
        result = _apply_minimax_constraints(kwargs)
        self.assertEqual(result["temperature"], 0.01)

    def test_clamp_temperature_negative(self):
        """Negative temperature should be clamped to 0.01."""
        kwargs = {"model": "minimax/MiniMax-M2.7", "temperature": -0.5}
        result = _apply_minimax_constraints(kwargs)
        self.assertEqual(result["temperature"], 0.01)

    def test_clamp_temperature_above_one(self):
        """Temperature > 1.0 should be clamped to 1.0."""
        kwargs = {"model": "minimax/MiniMax-M2.7", "temperature": 1.5}
        result = _apply_minimax_constraints(kwargs)
        self.assertEqual(result["temperature"], 1.0)

    def test_valid_temperature_unchanged(self):
        """Valid temperature (0 < t <= 1.0) should remain unchanged."""
        kwargs = {"model": "minimax/MiniMax-M2.7", "temperature": 0.7}
        result = _apply_minimax_constraints(kwargs)
        self.assertEqual(result["temperature"], 0.7)

    def test_temperature_one_unchanged(self):
        """Temperature 1.0 is valid and should remain unchanged."""
        kwargs = {"model": "minimax/MiniMax-M2.7", "temperature": 1.0}
        result = _apply_minimax_constraints(kwargs)
        self.assertEqual(result["temperature"], 1.0)

    def test_no_temperature_no_change(self):
        """When temperature is not set, no clamping should occur."""
        kwargs = {"model": "minimax/MiniMax-M2.7"}
        result = _apply_minimax_constraints(kwargs)
        self.assertNotIn("temperature", result)

    def test_remove_response_format(self):
        """response_format should be removed for MiniMax models."""
        kwargs = {
            "model": "minimax/MiniMax-M2.7",
            "response_format": {"type": "json_object"},
        }
        result = _apply_minimax_constraints(kwargs)
        self.assertNotIn("response_format", result)

    def test_other_params_preserved(self):
        """Non-constrained parameters should be preserved."""
        kwargs = {
            "model": "minimax/MiniMax-M2.7",
            "temperature": 0.5,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = _apply_minimax_constraints(kwargs)
        self.assertEqual(result["max_tokens"], 1024)
        self.assertEqual(result["messages"], [{"role": "user", "content": "Hello"}])
        self.assertEqual(result["temperature"], 0.5)


class TestProviderRegistry(unittest.TestCase):
    """Tests for MiniMax in the nanobot provider registry."""

    def test_minimax_in_registry(self):
        """MiniMax should be in the provider registry."""
        names = [entry[0] for entry in PROVIDER_REGISTRY]
        self.assertIn("minimax", names)

    def test_minimax_base_url(self):
        """MiniMax should have the correct base URL."""
        for name, _keywords, base_url in PROVIDER_REGISTRY:
            if name == "minimax":
                self.assertEqual(base_url, "https://api.minimax.io/v1")
                break

    def test_minimax_keyword_match(self):
        """MiniMax keyword should match 'minimax'."""
        for name, keywords, _base_url in PROVIDER_REGISTRY:
            if name == "minimax":
                self.assertIn("minimax", keywords)
                break

    def test_match_provider_minimax_model(self):
        """match_provider should find minimax config for minimax models."""
        providers = {
            "minimax": {"apiKey": "test-key-123"},
        }
        result = match_provider(providers, "minimax/MiniMax-M2.7")
        self.assertIsNotNone(result)
        self.assertEqual(result["api_key"], "test-key-123")
        self.assertEqual(result["api_base"], "https://api.minimax.io/v1")

    def test_match_provider_minimax_keyword(self):
        """match_provider should detect minimax in model name via keyword."""
        providers = {
            "minimax": {"apiKey": "test-key-456"},
        }
        result = match_provider(providers, "MiniMax-M2.7")
        self.assertIsNotNone(result)
        self.assertEqual(result["api_key"], "test-key-456")

    def test_match_provider_minimax_forced(self):
        """match_provider should use minimax when forced_provider='minimax'."""
        providers = {
            "minimax": {"apiKey": "test-key-789"},
            "openai": {"apiKey": "other-key"},
        }
        result = match_provider(providers, "some-model", forced_provider="minimax")
        self.assertIsNotNone(result)
        self.assertEqual(result["api_key"], "test-key-789")


class TestResolverMiniMaxDetection(unittest.TestCase):
    """Tests for MINIMAX_API_KEY auto-detection in the resolver."""

    @patch.dict(os.environ, {"MINIMAX_API_KEY": "minimax-test-key"}, clear=False)
    @patch("openspace.host_detection.nanobot.try_read_nanobot_config", return_value=None)
    def test_auto_detect_minimax_api_key(self, _mock_nanobot):
        """MINIMAX_API_KEY should be auto-detected for minimax models."""
        model, kwargs = build_llm_kwargs("minimax/MiniMax-M2.7")
        self.assertEqual(model, "minimax/MiniMax-M2.7")
        self.assertEqual(kwargs.get("api_key"), "minimax-test-key")
        self.assertEqual(kwargs.get("api_base"), "https://api.minimax.io/v1")

    @patch.dict(os.environ, {}, clear=False)
    @patch("openspace.host_detection.nanobot.try_read_nanobot_config", return_value=None)
    def test_no_minimax_key_no_kwargs(self, _mock_nanobot):
        """Without MINIMAX_API_KEY, no api_key should be set."""
        # Remove MINIMAX_API_KEY if it exists
        os.environ.pop("MINIMAX_API_KEY", None)
        model, kwargs = build_llm_kwargs("minimax/MiniMax-M2.7")
        self.assertEqual(model, "minimax/MiniMax-M2.7")
        self.assertNotIn("api_key", kwargs)

    @patch.dict(os.environ, {"MINIMAX_API_KEY": "minimax-key"}, clear=False)
    @patch("openspace.host_detection.nanobot.try_read_nanobot_config", return_value=None)
    def test_minimax_not_triggered_for_openai(self, _mock_nanobot):
        """MINIMAX_API_KEY should not be used for non-minimax models."""
        model, kwargs = build_llm_kwargs("openai/gpt-4o")
        self.assertEqual(model, "openai/gpt-4o")
        self.assertNotIn("api_key", kwargs)

    @patch.dict(
        os.environ,
        {"OPENSPACE_LLM_API_KEY": "explicit-key", "MINIMAX_API_KEY": "minimax-key"},
        clear=False,
    )
    @patch("openspace.host_detection.nanobot.try_read_nanobot_config", return_value=None)
    def test_explicit_key_overrides_minimax(self, _mock_nanobot):
        """OPENSPACE_LLM_API_KEY (Tier 1) should override MINIMAX_API_KEY (Tier 3)."""
        model, kwargs = build_llm_kwargs("minimax/MiniMax-M2.7")
        self.assertEqual(kwargs["api_key"], "explicit-key")


if __name__ == "__main__":
    unittest.main()
