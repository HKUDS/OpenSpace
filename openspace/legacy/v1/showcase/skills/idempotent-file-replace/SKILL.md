---
name: idempotent-file-replace
description: Read-then-diff before writing a file to ensure the write is skipped when the content already matches, preventing unnecessary modifications and downstream side effects.
---

# Idempotent File Replace

When tasked with **completely replacing** a file with specific content, always
perform a read-and-compare step before writing. Only write when the file
actually differs from the desired content.

## Why This Matters

| Risk of blind writes | Benefit of idempotent check |
|---|---|
| Triggers filesystem watchers / hot-reload loops | No spurious rebuild when content is unchanged |
| Wastes a tool call on a no-op | One extra read saves a write + all downstream costs |
| Pollutes VCS history with empty diffs | Clean commit history |
| Breaks CI caching layers | Stable mtimes keep caches valid |

---

## Procedure

### Step 1 — Read the current file

Always read the file first, even when you are confident about the replacement.

```
read_file(path="<target_file>")
```

If the file does **not yet exist**, treat its current content as an empty
string and proceed to Step 3.

---

### Step 2 — Compare current content to desired content

Perform an exact string comparison (whitespace-sensitive).

```python
current  = <content returned by read_file>
desired  = <full replacement content>

needs_write = (current.strip() != desired.strip())
# Use .strip() to tolerate a single trailing newline difference,
# or compare verbatim if byte-exact output is required.
```

Alternatively, a unified diff gives a human-readable explanation of what
would change and is useful for logging:

```python
import difflib, sys

diff = list(difflib.unified_diff(
    current.splitlines(keepends=True),
    desired.splitlines(keepends=True),
    fromfile="current",
    tofile="desired",
))

if not diff:
    print("Files are identical — skipping write.")
    needs_write = False
else:
    print("Diff detected:\n" + "".join(diff))
    needs_write = True
```

---

### Step 3 — Conditional write

```python
if needs_write:
    write_file(path="<target_file>", content=desired)
    print("File written.")
else:
    # Report completion immediately — no write needed.
    print("File already matches desired content. No action taken.")
```

---

## Decision Flowchart

```
Task: "Replace <file> with <content>"
        │
        ▼
   read_file(<file>)
        │
        ▼
  content == desired?
   ┌────┴────┐
  YES       NO
   │         │
   ▼         ▼
 SKIP     write_file(<file>, desired)
 write        │
   │          ▼
   └──► Report COMPLETE
```

---

## Complete Example

```python
TARGET = "src/components/Panel.ts"
DESIRED_CONTENT = """// Auto-generated — do not edit
export const Panel = () => { ... };
"""

# Step 1: read
current = read_file(path=TARGET)          # tool call

# Step 2: compare
if current.strip() == DESIRED_CONTENT.strip():
    print(f"{TARGET} already matches. Skipping write.")
    # → COMPLETE
else:
    # Step 3: write
    write_file(path=TARGET, content=DESIRED_CONTENT)   # tool call
    print(f"{TARGET} updated.")
    # → COMPLETE
```

---

## Edge Cases

| Situation | Handling |
|---|---|
| File does not exist | `read_file` raises / returns empty → treat as `""` → always write |
| Encoding differences | Normalize to UTF-8 before comparison |
| Line-ending differences (CRLF vs LF) | Normalize with `.replace("\r\n", "\n")` before comparing |
| Byte-exact requirement | Skip `.strip()` and compare verbatim |
| Large files | Compute SHA-256 hash of both strings for efficiency before full diff |

---

## Key Principle

> **Never assume a file needs to be written. One cheap read prevents an
> expensive (and potentially harmful) write.**

This pattern applies to any "replace entire file" task regardless of file
type: source code, configuration, templates, lock files, generated assets,
etc.