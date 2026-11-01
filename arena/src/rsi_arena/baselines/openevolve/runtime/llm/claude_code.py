"""
Claude Code CLI interface for LLMs.

Uses the Claude Code CLI (`claude -p`) as a non-interactive LLM backend,
enabling OpenEvolve to run with Anthropic's Claude models without requiring
direct API keys — authentication is handled by the CLI's OAuth session.

Usage in config.yaml:
    llm:
      provider: "claude_code"
      models:
        - name: "sonnet"
          weight: 1.0
          max_tokens: 16000
          timeout: 300

Or inject programmatically:
    from rsi_arena.baselines.openevolve.runtime.llm.claude_code import init_claude_code_client
    for model_cfg in config.llm.models:
        model_cfg.init_client = init_claude_code_client
"""

import asyncio
import logging
import subprocess
from typing import Dict, List, Optional

from rsi_arena.baselines.openevolve.runtime.llm.base import LLMInterface

logger = logging.getLogger(__name__)


class ClaudeCodeLLM(LLMInterface):
    """LLM interface that uses the Claude Code CLI for generation.

    Requires `claude` CLI to be installed and authenticated
    (run `claude login` first).
    """

    def __init__(self, model_cfg=None):
        self.model = getattr(model_cfg, "name", "sonnet")
        self.system_message = getattr(model_cfg, "system_message", None)
        self.max_tokens = getattr(model_cfg, "max_tokens", 16000)
        self.timeout = getattr(model_cfg, "timeout", 300)
        self.weight = getattr(model_cfg, "weight", 1.0)
        self.retries = getattr(model_cfg, "retries", 3)
        self.retry_delay = getattr(model_cfg, "retry_delay", 5)
        self.max_budget_usd = getattr(model_cfg, "max_budget_usd", 1.0)
        self.cwd = getattr(model_cfg, "cwd", None)
        logger.info(f"Initialized ClaudeCodeLLM with model: {self.model}")

    async def generate(self, prompt: str, **kwargs) -> str:
        sys_msg = kwargs.pop("system_message", self.system_message) or ""
        return await self.generate_with_context(
            system_message=sys_msg,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )

    async def generate_with_context(
        self, system_message: str, messages: List[Dict[str, str]], **kwargs
    ) -> str:
        user_content = "\n\n".join(
            m.get("content", "") for m in messages if m.get("role") == "user"
        )

        cmd = [
            "claude",
            "-p",
            "--model",
            self.model,
            "--no-session-persistence",
            "--output-format",
            "text",
        ]
        if system_message:
            cmd.extend(["--system-prompt", system_message])

        budget = kwargs.get("max_budget_usd", self.max_budget_usd)
        cmd.extend(["--max-budget-usd", str(budget)])

        cmd.append(user_content)

        timeout = kwargs.get("timeout", self.timeout)
        retries = kwargs.get("retries", self.retries)
        retry_delay = kwargs.get("retry_delay", self.retry_delay)

        loop = asyncio.get_event_loop()
        for attempt in range(retries + 1):
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: self._run_cli(cmd, timeout)),
                    timeout=timeout + 30,
                )
                return result
            except asyncio.TimeoutError:
                if attempt < retries:
                    logger.warning(
                        f"Claude Code CLI timeout on attempt {attempt + 1}/{retries + 1}. Retrying..."
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(f"All {retries + 1} attempts failed with timeout")
                    raise
            except Exception as e:
                if attempt < retries:
                    logger.warning(
                        f"Claude Code CLI error on attempt {attempt + 1}/{retries + 1}: {e}. Retrying..."
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(f"All {retries + 1} attempts failed with error: {e}")
                    raise

    def _run_cli(self, cmd: list, timeout: int) -> str:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.cwd,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if stderr:
                    logger.warning(f"Claude CLI stderr: {stderr[:500]}")
            output = result.stdout.strip()
            if not output:
                raise RuntimeError(f"Empty response from Claude CLI. stderr: {result.stderr[:500]}")
            return output
        except subprocess.TimeoutExpired:
            raise asyncio.TimeoutError("Claude CLI subprocess timed out")


def init_claude_code_client(model_cfg):
    """Factory function compatible with OpenEvolve's init_client config hook."""
    return ClaudeCodeLLM(model_cfg)
