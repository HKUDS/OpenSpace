"""
LLM module initialization
"""

from rsi_arena.baselines.openevolve.runtime.llm.base import LLMInterface
from rsi_arena.baselines.openevolve.runtime.llm.ensemble import LLMEnsemble
from rsi_arena.baselines.openevolve.runtime.llm.openai import OpenAILLM
from rsi_arena.baselines.openevolve.runtime.llm.claude_code import ClaudeCodeLLM, init_claude_code_client

__all__ = [
    "LLMInterface",
    "OpenAILLM",
    "ClaudeCodeLLM",
    "init_claude_code_client",
    "LLMEnsemble",
]
