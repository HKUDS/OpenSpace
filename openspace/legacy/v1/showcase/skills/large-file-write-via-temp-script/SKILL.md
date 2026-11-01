---
name: large-file-write-via-temp-script
description: Reliable fallback technique for writing large or special-character-heavy file content when inline write_file or shell_agent calls fail with unknown errors — uses a Python helper script written to a temp file to avoid escaping and truncation pitfalls.
---

# Large File Write via Temp Script

## Problem

Writing large files — especially TypeScript, TSX, or any content containing
Unicode emoji, backticks, `${}` template literals, or multi-line heredoc
conflicts — can silently fail or truncate content when passed inline.

**Failure modes observed:**
- `write_file` returns `[ERROR] unknown error` on large payloads
- `node -e '...'` with backtick template literals silently empties the target file
- `python3 -c '...'` breaks on unescaped quotes or special characters
- `shell_agent` may time out or fail when the inline content is too large

---

## Solution: Write a Python Script to a Temp File First

Instead of passing file content inline, write a small Python helper script to
`/tmp/write_task.py` (or similar), then execute it. The helper script holds
the target file content as a raw Python string or via base64 decoding.

---

## Approach A — Python Triple-Quoted String (preferred for readability)

Use `write_file` or a heredoc to create the helper, then run it:

```bash
# Step 1: Write the helper script to /tmp
cat > /tmp/write_task.py << 'PYEOF'
import pathlib

content = r"""
// Your TypeScript / file content goes here
// Backticks `like this`, ${template} literals, and emoji 🎉 are all safe
export const MyComponent = () => {
  const msg = `Hello, world!`;
  return <div>{msg}</div>;
};
"""

pathlib.Path("/path/to/target/file.ts").write_text(content.lstrip("\n"), encoding="utf-8")
print("Written successfully.")
PYEOF

# Step 2: Execute the helper
python3 /tmp/write_task.py
```

> **Why `r"""`?** A raw triple-quoted string avoids all backslash escaping
> issues. The only content that cannot appear verbatim is `"""` itself — if
> needed, split the string or escape that sequence only.

---

## Approach B — Base64-Encoded Content (most robust for binary/complex content)

When the content contains `"""` or other Python string edge cases:

```bash
# Step 1: Base64-encode your content (done once, outside the target system)
# python3 -c "import base64; print(base64.b64encode(open('file.ts').read().encode()).decode())"

# Step 2: Embed the base64 blob in the helper script
cat > /tmp/write_task.py << 'PYEOF'
import base64, pathlib

b64 = (
    "aW1wb3J0IFJlYWN0IGZyb20gJ3JlYWN0JzsKCmV4cG9ydCBjb25zdCBNeUNvbXBvbmVudCA9"
    "ICgpID0+IHsKICByZXR1cm4gPGRpdj5IZWxsbzwvZGl2PjsKfTsK"
    # ... more lines if needed
)

content = base64.b64decode(b64).decode("utf-8")
pathlib.Path("/path/to/target/file.ts").write_text(content, encoding="utf-8")
print(f"Written {len(content)} bytes successfully.")
PYEOF

python3 /tmp/write_task.py
```

---

## Approach C — shell_agent Delegation (simplest for agents)

Ask `shell_agent` to perform the write using the temp-script pattern:

> "Write the following TypeScript content to `/src/components/MyPanel.ts`.
> Do NOT use `node -e` or inline Python. Instead write a Python script to
> `/tmp/write_panel.py` using a triple-quoted raw string, then execute it."

---

## Decision Guide

| Situation | Recommended Approach |
|---|---|
| Content has backticks / `${}` template literals | Approach A (raw `r"""`) |
| Content has `"""` sequences | Approach B (base64) |
| Content is binary or non-UTF-8 | Approach B (base64) |
| Delegating to shell_agent | Approach C with explicit instructions |
| Quick small file | Direct `write_file` (no workaround needed) |

---

## Anti-Patterns to Avoid

```bash
# ❌ DANGEROUS — backtick template literals will break node -e
node -e "const fs = require('fs'); fs.writeFileSync('out.ts', \`${content}\`);"

# ❌ FRAGILE — breaks on unescaped quotes and special characters
python3 -c "open('out.ts','w').write('${content}')"

# ❌ UNRELIABLE for large payloads — may silently truncate
echo "${large_content}" > out.ts
```

---

## Verification Step

Always verify the write succeeded:

```bash
# Check file exists and has non-zero size
python3 -c "
import pathlib
p = pathlib.Path('/path/to/target/file.ts')
assert p.exists() and p.stat().st_size > 0, f'Write failed: {p}'
print(f'OK — {p.stat().st_size} bytes written to {p}')
"
```

---

## Notes

- The temp script approach works for **any file type**: `.ts`, `.tsx`, `.js`, `.json`, `.md`, `.py`, etc.
- Always `lstrip("\n")` the triple-quoted string to avoid a leading blank line.
- For very large files (>50 KB), prefer base64 to avoid any risk of heredoc conflicts.
- Clean up temp scripts after use: `rm /tmp/write_task.py`