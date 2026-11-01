---
name: unicode-safe-file-writing
description: How to reliably write files containing multi-byte Unicode (emoji, special symbols) when write_file fails with 'unknown error' by falling back to run_shell with a heredoc.
---

# Unicode-Safe File Writing

## Problem

The `write_file` tool may fail with `'unknown error'` when the file content
contains multi-byte Unicode characters such as:

- Emoji (e.g. `🚀`, `✅`, `❌`)
- Special typographic symbols (e.g. `→`, `•`, `—`, `©`)
- Non-ASCII characters outside the Latin-1 range

This is a known encoding limitation of `write_file`.

## Solution

Fall back to `run_shell` and write the file using a **shell heredoc**
(`cat > file << 'EOF' ... EOF`). The single-quoted delimiter `'EOF'`
prevents the shell from interpreting any special characters inside the
block.

---

## Step-by-Step Recovery

### Step 1 — Detect the failure

If `write_file` returns an error such as:

```
Error: unknown error
```

and the content contains non-ASCII characters, assume a Unicode encoding
issue and proceed to the heredoc fallback.

### Step 2 — Strip or replace problematic characters (if needed)

Before embedding content in the heredoc, decide whether to:

- **Keep** the characters as-is (works if the shell/terminal is UTF-8 aware)
- **Replace** emoji/symbols with ASCII equivalents (safest, most portable)
- **Remove** them entirely

Common replacements:

| Original | Replacement |
|----------|-------------|
| `✅`     | `[OK]`      |
| `❌`     | `[ERROR]`   |
| `🚀`     | `[START]`   |
| `→`      | `->`        |
| `•`      | `-`         |
| `—`      | `--`        |

### Step 3 — Write the file via heredoc

Use `run_shell` with the following pattern:

```bash
cat > path/to/file.ext << 'EOF'
...file content here (with Unicode stripped/replaced if needed)...
EOF
```

**Key details:**
- Use `'EOF'` (single-quoted) as the heredoc delimiter — this disables all
  shell variable expansion and special character interpretation inside the block.
- Do **not** indent the closing `EOF` (it must start at column 0).
- For deeply nested content, choose a delimiter that cannot appear in the
  content itself (e.g. `'HEREDOC_END'`, `'FILEEND'`).

---

## Example

### Failing call

```python
write_file(
    path="src/components/StatusPanel.ts",
    content="// Status icons\nconst icons = { ok: '✅', err: '❌', run: '🚀' };\n"
)
# → Error: unknown error
```

### Recovery via heredoc

```bash
cat > src/components/StatusPanel.ts << 'EOF'
// Status icons
const icons = { ok: '[OK]', err: '[ERROR]', run: '[START]' };
EOF
```

Or, if UTF-8 output is acceptable in the environment:

```bash
cat > src/components/StatusPanel.ts << 'EOF'
// Status icons
const icons = { ok: '✅', err: '❌', run: '🚀' };
EOF
```

---

## Multi-File Scenario

When writing several files and one fails, write the others with `write_file`
as normal and apply the heredoc fallback only to the failing file(s):

```bash
# Write multiple files that contain Unicode
cat > src/utils/icons.ts << 'EOF'
export const ICONS = {
  success: '[OK]',
  failure: '[FAIL]',
  pending: '[...]',
};
EOF

cat > src/utils/labels.ts << 'EOF'
export const LABELS = {
  title: 'Dashboard',
  subtitle: 'Real-time status',
};
EOF
```

---

## Important Notes

1. **Always prefer `write_file` first** — only fall back to the heredoc
   approach when `write_file` fails.
2. **Verify after writing** — use `run_shell` with `cat path/to/file` or
   `head -5 path/to/file` to confirm the content was written correctly.
3. **Directory creation** — `write_file` auto-creates parent directories;
   the heredoc approach does not. Pre-create directories with
   `mkdir -p path/to/dir` if needed.
4. **Large files** — heredocs work well for files of any practical size;
   there is no meaningful shell limit.
5. **Escaping backslashes** — inside a single-quoted heredoc, backslashes
   are treated literally (no escaping needed), which is usually desirable.