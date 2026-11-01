---
name: write-special-chars-readback
description: Workaround for write_file failures caused by special characters (apostrophes, backticks, template literals) using a Python heredoc script, with mandatory post-write verification via read_file and cleanup of /tmp helper scripts.
---

# Write File Special Characters Workaround (Verified)

## Problem

The `write_file` tool may fail with `[ERROR] unknown error` when file content
contains special characters such as:

- Apostrophes / single quotes (`'`)
- Backticks (`` ` ``)
- Template literals (`` `${variable}` ``) common in TypeScript/JavaScript
- Complex combinations of quotes and escape sequences

This is especially common when writing TypeScript, JavaScript, shell scripts,
or any source file with string interpolation syntax.

## Solution: Python Heredoc Workaround

Write a small Python script to `/tmp/` that uses triple-quoted strings to
embed the file content, then execute it with `python3`.

### Step-by-Step

1. **Compose a Python writer script** using triple-quoted strings (`"""..."""`).
2. **Write the Python script itself to `/tmp/`** — its content is plain enough
   that `write_file` will succeed (no problematic characters in the wrapper).
3. **Execute** the script with `run_shell`: `python3 /tmp/write_<name>.py`
4. **Verify** the written file with `read_file` to confirm contents are correct.
5. **Clean up** the `/tmp` helper script: `rm /tmp/write_<name>.py`

### Escaping Rules Inside the Python Script

| Character | How to handle |
|-----------|--------------|
| Backslash `\` | Escape as `\\` |
| Triple double-quote `"""` | Escape as `\"\"\"` or use `'''` strings instead |
| Everything else (backticks, `$`, `'`, `{}`) | No escaping needed |

### Template

```python
#!/usr/bin/env python3
content = """
<YOUR FILE CONTENT HERE>
""".lstrip("\n")

with open("/path/to/target/file.ts", "w") as f:
    f.write(content)

print("File written successfully.")
```

### Concrete Example

Suppose you need to write a TypeScript file with template literals and
apostrophes that causes `write_file` to fail:

**Step 1 — Write the Python helper to `/tmp/`:**

Use `write_file` with path `/tmp/write_greeting.py` and content:

```python
#!/usr/bin/env python3
content = """
export function greet(name: string): string {
  const msg = `Hello, ${name}! It's a great day.`;
  console.log(`Greeting: ${msg}`);
  return msg;
}
""".lstrip("\n")

with open("/app/src/greeting.ts", "w") as f:
    f.write(content)

print("Written: /app/src/greeting.ts")
```

**Step 2 — Execute the helper:**

```bash
python3 /tmp/write_greeting.py
```

**Step 3 — Verify with `read_file`:**

Use `read_file` with path `/app/src/greeting.ts` to confirm the file content
is exactly what was intended. This catches silent truncation or encoding
issues that shell `cat` output might obscure.

**Step 4 — Clean up the helper:**

```bash
rm /tmp/write_greeting.py
```

This keeps `/tmp` tidy and prevents stale helper scripts from being confused
with current ones on subsequent runs.

## Standard Post-Write Checklist

After every use of this pattern, always complete **all three** of these steps:

| Step | Action | Why |
|------|---------|-----|
| ✅ Execute | `python3 /tmp/write_<name>.py` | Writes the target file |
| ✅ Verify | `read_file` on the target path | Confirms content integrity |
| ✅ Clean up | `rm /tmp/write_<name>.py` | Prevents stale helper accumulation |

Skipping verification risks silently shipping a truncated or malformed file.
Skipping cleanup risks confusion when the same helper name is reused later.

## When to Use This Pattern

- `write_file` returns `[ERROR] unknown error` for a specific file.
- The file contains TypeScript/JavaScript template literals.
- The file contains mixed quote styles or shell-like special characters.
- Any time direct file writing fails and content cannot be easily sanitized.

## Multiple Files

For writing several problematic files in one pass, consolidate them into a
single Python script:

```python
#!/usr/bin/env python3
import os

files = {
    "/app/src/component.tsx": """
import React from 'react';

const App = () => (
  <div className={`container`}>
    <h1>It's working!</h1>
  </div>
);

export default App;
""".lstrip("\n"),

    "/app/src/utils.ts": """
export const format = (val: number) => `Value: ${val.toFixed(2)}`;
""".lstrip("\n"),
}

for path, content in files.items():
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    print(f"Written: {path}")
```

After running the multi-file script:

1. Use `read_file` on **each** target path to verify all files were written
   correctly.
2. Then clean up: `rm /tmp/write_<name>.py`

## Key Advantages

- **No shell escaping required** — Python triple-quoted strings are literal.
- **Handles all TypeScript syntax** — template literals, generics, JSX, etc.
- **Atomic** — the target file is only created if the Python script succeeds.
- **Debuggable** — the intermediate `.py` file can be inspected if needed.
- **Cleanup (required):** `rm /tmp/write_*.py` after all files are written —
  this is a mandatory step, not optional.
- **Verified** — always follow up with `read_file` on the target file to
  confirm content integrity before considering the task complete.
