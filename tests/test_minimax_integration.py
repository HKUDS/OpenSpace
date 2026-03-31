"""Integration tests for MiniMax provider support.

These tests require MINIMAX_API_KEY to be set in the environment.
They are automatically skipped when the key is not available.
"""

import asyncio
import os
import unittest

MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY")
SKIP_REASON = "MINIMAX_API_KEY not set"


@unittest.skipUnless(MINIMAX_API_KEY, SKIP_REASON)
class TestMiniMaxChatIntegration(unittest.TestCase):
    """Integration tests for MiniMax chat completions via litellm."""

    def test_basic_chat_completion(self):
        """MiniMax M2.7 should return a valid chat completion."""
        import litellm

        response = litellm.completion(
            model="minimax/MiniMax-M2.7",
            messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
            max_tokens=50,
            temperature=0.5,
            api_key=MINIMAX_API_KEY,
        )
        self.assertTrue(response.choices)
        content = response.choices[0].message.content
        self.assertIsNotNone(content)

    def test_highspeed_model(self):
        """MiniMax M2.7-highspeed should also work."""
        import litellm

        response = litellm.completion(
            model="minimax/MiniMax-M2.7-highspeed",
            messages=[{"role": "user", "content": "Reply with only the word 'ok'."}],
            max_tokens=10,
            temperature=0.5,
            api_key=MINIMAX_API_KEY,
        )
        self.assertTrue(response.choices)
        self.assertTrue(response.choices[0].message.content)

    def test_llm_client_with_minimax(self):
        """LLMClient should work with MiniMax models (temperature clamping applied)."""
        from openspace.llm.client import LLMClient

        client = LLMClient(
            model="minimax/MiniMax-M2.7-highspeed",
            timeout=30.0,
            max_retries=1,
            api_key=MINIMAX_API_KEY,
        )

        result = asyncio.get_event_loop().run_until_complete(
            client.complete(
                messages=[{"role": "user", "content": "Reply with 'integration test passed'."}],
                temperature=0.5,
                max_tokens=30,
            )
        )
        self.assertIn("message", result)
        content = result["message"].get("content", "")
        self.assertTrue(content)


if __name__ == "__main__":
    unittest.main()
