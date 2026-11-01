---
name: write-special-chars-ts-check
description: Workaround for write_file failures caused by special characters (apostrophes, backticks, template literals) using a Python heredoc script, plus a reliable TypeScript type-checking step that filters output to only the newly-written files to avoid being misled by pre-existing errors.
---

# Write File Special Characters Workaround (with TypeScript Verification)

## Problem

The `write_file` tool may fail with `[ERROR] unknown error` when file content
contains special characters such as:

- Apostrophes / single quotes (`'`)
- Backticks (`` ` ``)
- Template literals (`` `${variable}` ``) common in TypeScript/JavaScript
- Complex combinations of quotes and escape sequences

This is especially common when writing TypeScript, JavaScript, shell scripts,
or any source file with string interpolation syntax.

Additionally, after writing TypeScript files, you need to verify they are
type-correct — but common methods like `npx tsc` or `./node_modules/.bin/tsc`
can fail with `[ERROR] unknown error` due to timeout or symlink resolution
issues. The reliable method is to call the TypeScript binary directly.

## Solution: Python Heredoc Workaround + Direct TSC Verification

### Phase 1: Write Files via Python Heredoc

Write a small Python script to `/tmp/` that uses triple-quoted strings to
embed the file content, then execute it with `python3`.

#### Step-by-Step

1. **Compose a Python writer script** using triple-quoted strings (`"""..."""`).
2. **Write the Python script itself to `/tmp/`** — its content is plain enough
   that `write_file` will succeed (no problematic characters in the wrapper).
3. **Execute** the script with `run_shell`: `python3 /tmp/write_<name>.py`

#### Escaping Rules Inside the Python Script

| Character | How to handle |
|-----------|--------------|
| Backslash `\` | Escape as `\\` |
| Triple double-quote `"""` | Escape as `\"\"\"` or use `'''` strings instead |
| Everything else (backticks, `$`, `'`, `{}`) | No escaping needed |

#### Template

```python
#!/usr/bin/env python3
content = """
<YOUR FILE CONTENT HERE>
""".lstrip("\n")

with open("/path/to/target/file.ts", "w") as f:
    f.write(content)

print("File written successfully.")
```

#### Concrete Example

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

**Step 3 — Verify the file was created:**

```bash
cat /app/src/greeting.ts
```

### Phase 2: TypeScript Type-Checking (Reliable Method)

After writing TypeScript files, always verify they introduce zero new type
errors. Use the direct binary path — **not** `npx` or `.bin` symlinks, which
are prone to timeout and path-resolution failures.

#### The Reliable TSC Command

```bash
node node_modules/typescript/bin/tsc --noEmit 2>&1 | grep "src/greeting.ts"
```

Key points:
- `node node_modules/typescript/bin/tsc` — invokes TypeScript directly via
  `node`, bypassing npx overhead and symlink resolution entirely.
- `--noEmit` — type-checks only; does not write output files.
- `2>&1` — merges stderr into stdout so grep can filter everything.
- `| grep "<filename>"` — filters output to only lines mentioning your
  newly-written file(s), so pre-existing errors in other files do not
  create false alarms.

#### Why Not npx or .bin Symlinks?

| Method | Risk |
|--------|------|
| `npx tsc --noEmit` | Frequently returns `[ERROR] unknown error` (timeout, network, PATH issues) |
| `./node_modules/.bin/tsc --noEmit` | Symlink resolution can fail in some environments |
| `node node_modules/typescript/bin/tsc --noEmit` | **Reliable** — direct JS execution, no symlinks, no network |

#### Filtering for Multiple New Files

When you've written several files, pipe through a more specific grep pattern:

```bash
node node_modules/typescript/bin/tsc --noEmit 2>&1 | grep -E "src/(greeting|utils|component)\.ts"
```

Or use a directory prefix to catch all new files in one folder:

```bash
node node_modules/typescript/bin/tsc --noEmit 2>&1 | grep "src/myfeature/"
```

#### Interpreting the Output

- **No output / empty result** — grep found no errors in your new files. ✅
  The files are type-correct (pre-existing errors elsewhere are unrelated).
- **Lines printed** — type errors exist in your new files. ❌ Fix and re-check.

Example clean output (no new errors):
```
(no output)
```

Example with errors:
```
src/greeting.ts(3,14): error TS2345: Argument of type 'number' is not assignable to parameter of type 'string'.
```

## Full End-to-End Example

### Writing multiple TypeScript files and verifying them

**Step 1 — Write the Python helper to `/tmp/`:**

Use `write_file` with path `/tmp/write_settings_files.py`:

```python
#!/usr/bin/env python3
import os

files = {
    "/app/src/settings/settings-keys.ts": """
export const SETTINGS_KEYS = {
  theme: 'app.theme',
  language: 'app.language',
} as const;

export type SettingKey = keyof typeof SETTINGS_KEYS;
""".lstrip("\n"),

    "/app/src/settings/settings-store.ts": """
import { SETTINGS_KEYS, SettingKey } from './settings-keys';

export class SettingsStore {
  private data: Record<string, string> = {};

  get(key: SettingKey): string | undefined {
    return this.data[SETTINGS_KEYS[key]];
  }

  set(key: SettingKey, value: string): void {
    this.data[SETTINGS_KEYS[key]] = value;
  }
}
""".lstrip("\n"),
}

for path, content in files.items():
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    print(f"Written: {path}")
```

**Step 2 — Execute the helper:**

```bash
python3 /tmp/write_settings_files.py
```

**Step 3 — Verify file contents:**

```bash
cat /app/src/settings/settings-keys.ts
cat /app/src/settings/settings-store.ts
```

**Step 4 — Type-check only the new files:**

```bash
cd /app && node node_modules/typescript/bin/tsc --noEmit 2>&1 | grep "src/settings/"
```

Expected output if all is well: *(empty — no errors in the new files)*

**Step 5 — Optional cleanup:**

```bash
rm /tmp/write_settings_files.py
```

## When to Use This Pattern

- `write_file` returns `[ERROR] unknown error` for a specific file.
- The file contains TypeScript/JavaScript template literals.
- The file contains mixed quote styles or shell-like special characters.
- Any time direct file writing fails and content cannot be easily sanitized.
- After writing TypeScript files and needing reliable type-checking.

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

## Key Advantages

- **No shell escaping required** — Python triple-quoted strings are literal.
- **Handles all TypeScript syntax** — template literals, generics, JSX, etc.
- **Atomic** — the target file is only created if the Python script succeeds.
- **Debuggable** — the intermediate `.py` file can be inspected if needed.
- **Reliable type-checking** — direct `node` invocation avoids npx/symlink failures.
- **Targeted verification** — grep filtering isolates errors in new files only,
  preventing pre-existing project errors from masking or confusing results.
- **Cleanup (optional):** `rm /tmp/write_*.py` after all files are written.
