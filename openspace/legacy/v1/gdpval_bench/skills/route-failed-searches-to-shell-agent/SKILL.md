---
name: route-failed-searches-to-shell-agent
description: Delegate web search tasks to shell_agent when direct search_web tool fails, using its retry mechanism and multi-step capabilities for resilient data gathering
---

# Route Failed Web Searches to Shell Agent

## When to Use This Skill

Use this workflow when:
- Direct `search_web` tool calls consistently fail with 'unknown error' or similar failures
- You need to gather web-based information but the standard search tool is unreliable
- The task requires multi-step data collection that benefits from autonomous retry logic
- You need resilient data gathering that can adapt when initial approaches fail

## Core Pattern

Instead of repeatedly calling `search_web` when it fails, delegate the entire search task to `shell_agent`. The shell_agent's internal capabilities provide:
- **Automatic retry logic** with error detection and recovery
- **Multi-step approach** - can try different methods (APIs, scraping, alternative sources)
- **Code-based solutions** - can write Python scripts, use curl, or other tools
- **Autonomous adaptation** - figures out the best approach without manual intervention

## Step-by-Step Instructions

### Step 1: Detect Search Tool Failure

When `search_web` returns errors like:
- "unknown error"
- Empty or incomplete results
- Consistent failures across multiple queries

Recognize this as a signal to switch strategies rather than retrying the same failing tool.

### Step 2: Delegate to Shell Agent

Create a task for shell_agent with clear objectives:

```python
shell_agent(
    task="Gather current energy market data including oil prices, gas prices, and bond market information. Search multiple sources and compile findings into a structured summary.",
    timeout=300
)
```

**Key task formulation principles:**
- Specify the **what** (data needed) not the **how** (specific tools to use)
- Include success criteria and output format expectations
- Allow sufficient timeout for multi-step execution (300+ seconds)
- Let shell_agent decide whether to use Python, curl, or other approaches

### Step 3: Verify Results

Check shell_agent output for:
- Complete data collection (not partial results)
- Multiple sources cited (indicates thorough searching)
- Structured, usable output format
- Evidence of error handling (mentions of retry attempts, alternative approaches tried)

### Step 4: Extract and Use Data

Process the shell_agent results for your downstream task:
- Parse collected data into required format
- Cross-reference with any partial results from failed search_web calls
- Document sources for verification if needed

## Code Example

**Scenario**: Gathering market data for a financial report when search_web fails.

```python
# Failed approach - don't keep retrying this:
try:
    results = search_web(query="current oil gas bond market prices 2025")
    # This fails with unknown error
except:
    pass  # Don't just catch and retry the same failing tool

# Correct approach - delegate to shell_agent:
from tools import shell_agent

market_data = shell_agent(
    task="""
    Gather comprehensive energy market data for a trading strategy report.
    Required information:
    1. Current crude oil prices (WTI, Brent)
    2. Natural gas prices
    3. Relevant bond issuer analysis and yields
    4. Recent market trends and forecasts
    
    Use multiple sources, handle any errors by trying alternative approaches,
    and provide structured output with source citations.
    """,
    timeout=300
)

# Now use market_data for your report
```

## Why This Works

| Aspect | search_web (failing) | shell_agent (working) |
|--------|---------------------|----------------------|
| Retry logic | None or limited | Built-in multi-round retry |
| Error handling | Returns error | Attempts to fix and continue |
| Approach flexibility | Single method | Can use Python, bash, APIs, scraping |
| Adaptation | Fails completely | Tries alternative strategies |
| Output reliability | Inconsistent | Structured, verified results |

## Best Practices

1. **Don't chain failed tools**: Once search_web fails twice, switch to shell_agent immediately rather than accumulating more failures.

2. **Set appropriate timeouts**: Shell_agent needs time to iterate (200-300 seconds typical for complex searches).

3. **Be specific about data needs**: Shell_agent performs better with clear requirements than broad queries.

4. **Trust the autonomy**: Don't micromanage which tools shell_agent should use - let it decide the best approach.

5. **Validate outputs**: Always verify shell_agent results meet your needs before proceeding to downstream tasks.

## Troubleshooting

**Shell_agent also fails?**
- Increase timeout (try 400-500 seconds)
- Break the task into smaller sub-tasks
- Specify more concrete data sources or APIs to try

**Results are incomplete?**
- Add explicit output format requirements
- Request source citations to verify coverage
- Consider running multiple targeted shell_agent calls for different data categories

**Task takes too long?**
- Set clear timeout boundaries
- Prioritize most critical data in the task description
- Consider running parallel shell_agent calls for independent data categories

## Related Patterns

- Combine with `execute_code_sandbox` for custom data processing after shell_agent gathers raw data
- Use alongside `create_file` to persist collected data for downstream tasks
- Pair with `read_webpage` for targeted extraction when shell_agent identifies specific URLs