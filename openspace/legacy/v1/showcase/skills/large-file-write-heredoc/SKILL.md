---
name: large-file-write-heredoc
description: Reliable fallback technique for writing large file contents when write_file and shell_agent both fail with 'unknown error' due to payload size limits — uses run_shell with a Python heredoc to bypass tool constraints.
---

# Large File Write Fallback

## Problem

When writing large files (typically several KB or more), two common tools may
fail with `unknown error` due to internal payload size limits:

- `write_file` — has a maximum content size it can handle in a single call.
- `shell_agent` — may also hit payload limits when the task description
  includes large inline content.

## Solution

Use `run_shell` with a **Python heredoc** pattern. This streams the file
content through stdin directly into Python's `open()`, bypassing the payload
constraints of the other tools.

## Template

```
python3 - << 'EOF'
content = """<FILE CONTENTS HERE>"""
with open("<TARGET PATH>", "w") as f:
    f.write(content)
EOF
```

Pass this as the `command` parameter to `run_shell`.

## Step-by-Step Instructions

1. **Attempt the normal write** using `write_file` first. If it succeeds, you
   are done.

2. **If `write_file` fails** (especially with `unknown error` or a timeout on
   large content), do NOT retry with `shell_agent` using inline content —
   it will likely fail for the same reason.

3. **Use the `run_shell` heredoc fallback:**

   - Embed the full file content inside a Python triple-quoted string.
   - Specify the target path inside the `open()` call.
   - Pass the entire block as the `command` to `run_shell`.

4. **Escape carefully** inside the heredoc:
   - Backslashes that should be literal in the file must be doubled (`\\`).
   - Triple-quotes inside the content must be escaped (`\"\"\"`).
   - The heredoc delimiter `EOF` must not appear on a line by itself inside
     the content (rename it to `PYEOF` or `FILEEOF` if needed).

5. **Verify the write** by following up with a `run_shell` call such as:
   ```bash
   wc -l <TARGET PATH> && head -5 <TARGET PATH>
   ```

## Full Example

Suppose you need to write a large TypeScript file to
`src/components/Dashboard.ts`:

```
python3 - << 'PYEOF'
content = """import { foo } from './foo';

export interface DashboardData {
  title: string;
  items: string[];
}

export function createDashboard(data: DashboardData): string {
  return `<div>${data.title}</div>`;
}
"""
with open("src/components/Dashboard.ts", "w") as f:
    f.write(content)
PYEOF
```

Pass the above (without the surrounding code fence) as the `command`
argument to `run_shell`.

## When to Use This Pattern

| Situation | Recommended tool |
|---|---|
| Small file (< ~2 KB) | `write_file` |
| Medium file, no errors yet | `write_file` (try first) |
| Large file or `write_file` failed | `run_shell` + Python heredoc |
| `shell_agent` also fails on large inline content | `run_shell` + Python heredoc |

## Notes

- This pattern works for **any text-based file** (TypeScript, Python, JSON,
  YAML, Markdown, etc.).
- For binary files, adapt the approach to use `base64` decoding inside the
  Python script.
- The heredoc delimiter (`EOF`, `PYEOF`, `FILEEOF`) can be any string not
  present as a standalone line in your content — choose accordingly.
- This technique is also useful when content contains characters that would
  need heavy shell escaping in a plain `echo` or `printf` approach.