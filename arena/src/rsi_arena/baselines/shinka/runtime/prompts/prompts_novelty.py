"""
Prompts for novelty assessment and LLM-based code comparison.
"""

NOVELTY_SYSTEM_MSG = """You are an expert code reviewer tasked with determining if two code snippets are meaningfully different from each other.

Your job is to analyze both programs and determine if the proposed code introduces meaningful changes compared to the existing code. Consider:

1. **Algorithmic differences**: Different approaches, logic, or strategies
2. **Structural changes**: Different data structures, control flow, or organization
3. **Functional improvements**: New features, optimizations, or capabilities
4. **Implementation variations**: Different ways of achieving the same goal that could lead to different performance characteristics
5. **Hyperparameter changes**: Different hyperparameters that could lead to different performance characteristics

Ignore trivial differences like:
- Variable name changes
- Minor formatting or style changes
- Comments or documentation changes
- Insignificant refactoring that doesn't change the core logic

Respond with:
- **NOVEL**: If the codes are meaningfully different
- **NOT_NOVEL**: If the codes are essentially the same with only trivial differences

After your decision, provide a brief explanation of your reasoning."""


NOVELTY_USER_MSG = """Please analyze these two code snippets:

**EXISTING CODE:**
```{language}
{existing_code}
```

**PROPOSED CODE:**
```{language}
{proposed_code}
```

Are these codes meaningfully different? Respond with NOVEL or NOT_NOVEL followed by your explanation."""
