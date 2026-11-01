---
name: debug-sandbox-execution
description: Debug Python code execution failures by capturing partial traces, isolating failing functions, and incrementally verifying outputs
---

# Debug Sandbox Execution Failures

When `execute_code_sandbox` fails with unknown errors or incomplete output, use this debugging pattern to identify the root cause and recover incrementally.

## Problem

The `execute_code_sandbox` tool may fail silently, truncate output, or produce opaque errors. Complex scripts with multiple file outputs are especially prone to partial failures.

## Solution

Use a three-phase debugging approach:

### Phase 1: Capture Partial Execution Traces

When a sandbox execution fails, rerun the code using `run_shell` with output piping to capture whatever output is produced before the failure:

```bash
python your_script.py 2>&1 | head -100
```

This reveals:
- Which functions/steps executed successfully
- Where the failure occurred
- Any error messages that were suppressed

### Phase 2: Isolate Failing Functions

Break the script into smaller, testable units. Execute each function or code block independently:

```python
# Test individual components
if __name__ == "__main__":
    # Step 1: Test imports
    import numpy as np
    print("Imports OK")
    
    # Step 2: Test function A in isolation
    result_a = function_a()
    print(f"Function A: {result_a}")
    
    # Step 3: Test function B
    result_b = function_b(result_a)
    print(f"Function B: {result_b}")
```

Run each section with `execute_code_sandbox` separately to identify which component fails.

### Phase 3: Incremental Output Generation

Generate output files one at a time, verifying each before proceeding:

```python
import numpy as np
import soundfile as sf

# Generate and save file 1
audio1 = np.random.randn(48000 * 10).astype(np.float32)
sf.write('output_01.wav', audio1, 48000, subtype='FLOAT')

# Verify file 1 exists and has expected properties
import os
assert os.path.exists('output_01.wav'), "File 1 not created"

# Generate and save file 2
audio2 = np.random.randn(48000 * 10).astype(np.float32)
sf.write('output_02.wav', audio2, 48000, subtype='FLOAT')

# Verify file 2
assert os.path.exists('output_02.wav'), "File 2 not created"
```

## Example Workflow

1. **Initial attempt**: Run full script with `execute_code_sandbox`
2. **On failure**: Rerun with `run_shell` and `| head -100` to see partial output
3. **Identify breakpoint**: Find the last successful operation
4. **Split script**: Create separate scripts for each major section
5. **Test incrementally**: Run each section, verify outputs, proceed to next
6. **Combine successful sections**: Once all pieces work, combine into final script

## Best Practices

- **Always verify file creation** immediately after writing: `assert os.path.exists(path)`
- **Check file properties** (size, duration, format) before assuming success
- **Use print statements liberally** to mark progress through the script
- **Save intermediate outputs** so failures don't require restarting from scratch
- **Test audio/video generation** with short samples first (1-2 seconds) before full-length content

## When to Use

- Complex scripts with multiple file outputs
- Audio/video generation pipelines
- Scripts with external library dependencies
- Any `execute_code_sandbox` call that produces incomplete or no output