---
name: fallback-script-execution
description: Two-step script execution workflow for debugging when shell_agent and execute_code_sandbox consistently fail
---

# Fallback Script Execution with write_file + run_shell

## When to Use This Skill

Use this pattern when:
- `shell_agent` fails repeatedly with unclear error messages
- `execute_code_sandbox` consistently errors or times out
- You need better visibility into what's happening during execution
- Debugging inline code or delegated agents proves difficult

## Core Pattern

Instead of delegating execution to an agent or running inline code, use this two-step approach:

1. **Write script to file** using `write_file`
2. **Execute script** using `run_shell` with `python script.py`

This provides:
- Clearer error messages (full stack traces visible in run_shell output)
- Easier debugging (script persists for inspection)
- Better control over execution environment
- Ability to modify and re-run without rewriting code

## Step-by-Step Instructions

### Step 1: Write the Script File

Use `write_file` to create a self-contained Python script:

```
write_file with:
  path: "path/to/script_name.py"
  content: |
    #!/usr/bin/env python3
    # Your complete script here
    # Include imports, logic, and error handling
```

**Best Practices:**
- Include descriptive comments
- Add try/except blocks for error handling
- Print intermediate results for debugging
- Use absolute or clear relative paths

### Step 2: Execute the Script

Use `run_shell` to execute the script:

```
run_shell with:
  command: "python path/to/script_name.py"
```

**Best Practices:**
- Capture and examine full output
- If errors occur, the script file is still available for inspection
- You can re-run with modifications without starting over

## Example: Data Processing Task

### ❌ Problematic Approach (shell_agent fails repeatedly)

```
shell_agent with:
  task: "Load Excel file, calculate correlations, save results"
```

*Result: Agent struggles with path handling, unclear errors*

### ✅ Recommended Approach (write_file + run_shell)

```
# Step 1: Write script
write_file with:
  path: "correlation_analysis.py"
  content: |
    import pandas as pd
    import sys
    
    try:
        # Load data
        df = pd.read_excel('data.xlsx', sheet_name='Returns')
        print(f"Loaded {len(df)} rows")
        
        # Calculate correlation
        corr = df.corr()
        print(f"Correlation matrix shape: {corr.shape}")
        
        # Save results
        with pd.ExcelWriter('output.xlsx') as writer:
            df.to_excel(writer, sheet_name='Returns')
            corr.to_excel(writer, sheet_name='Correlation')
        
        print("SUCCESS: output.xlsx created")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

# Step 2: Execute script
run_shell with:
  command: "python correlation_analysis.py"
```

## Debugging Tips

1. **Add print statements** at key points to trace execution
2. **Check file paths** - use `run_shell` with `ls -la path/` to verify files exist
3. **Inspect errors** - run_shell output shows full Python stack traces
4. **Modify and re-run** - edit the script file and execute again without rewriting

## When to Escalate

If this pattern also fails:
- Verify Python is available: `run_shell` with `which python` or `python --version`
- Check file permissions: `run_shell` with `ls -la script.py`
- Try explicit Python path: `run_shell` with `/usr/bin/python script.py`
- Consider task complexity - may need to break into smaller scripts