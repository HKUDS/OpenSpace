---
name: typescript-compile-check-resilient
description: TypeScript compile-checking in projects where npx and node_modules/.bin symlinks fail, with a filtering technique to isolate errors from only newly-created or task-specific files — ignoring pre-existing legacy errors.
---

# TypeScript Compile-Check (Resilient) with File-Scoped Error Filtering

Use this workflow when you need to validate TypeScript changes in a project but the standard `npx tsc` or `node_modules/.bin/tsc` invocations fail due to broken symlinks, missing PATH entries, or environment issues.

---

## When to Use This Skill

- `npx tsc` fails with "command not found" or permission errors
- `node_modules/.bin/tsc` symlink is broken or unresolvable
- You need to validate only *newly-created* or *task-specific* files without being blocked by pre-existing compile errors in the broader codebase

---

## Step 1 — Invoke tsc Directly via node_modules

Bypass `npx` and symlinks entirely by calling the TypeScript binary directly:

```bash
node_modules/typescript/bin/tsc --noEmit 2>&1; echo EXIT:$?
```

**Why this works:** The `tsc` CLI entry point is a plain Node.js script at `node_modules/typescript/bin/tsc`. It does not depend on symlinks or PATH resolution. The `--noEmit` flag checks types without writing output files. Capturing `EXIT:$?` lets you programmatically detect success (`EXIT:0`) vs failure (`EXIT:1` or higher).

If the project uses a custom `tsconfig`, specify it explicitly:

```bash
node_modules/typescript/bin/tsc --noEmit --project tsconfig.json 2>&1; echo EXIT:$?
```

---

## Step 2 — Filter Output to Task-Specific Files Only

When a legacy codebase has pre-existing errors, the full `tsc` output will be noisy and may cause false negatives. Pipe through `grep` to isolate errors from only the files you created or modified:

```bash
node_modules/typescript/bin/tsc --noEmit 2>&1 \
  | grep -E "(file-pattern)" ; echo EXIT:${PIPESTATUS[0]}
```

### Examples

**Single new file:**
```bash
node_modules/typescript/bin/tsc --noEmit 2>&1 \
  | grep -E "settings-store\.ts"
```

**Multiple new files (pipe-delimited pattern):**
```bash
node_modules/typescript/bin/tsc --noEmit 2>&1 \
  | grep -E "(settings-keys|settings-store|preferences|SettingsModal)"
```

**All files in a specific directory:**
```bash
node_modules/typescript/bin/tsc --noEmit 2>&1 \
  | grep -E "src/features/my-feature/"
```

**Check: no output = no errors in your files (success).**

---

## Step 3 — Capture Exit Code Correctly Through a Pipe

When grepping, the shell exit code reflects `grep`, not `tsc`. Use `${PIPESTATUS[0]}` (bash) to recover the original `tsc` exit code:

```bash
node_modules/typescript/bin/tsc --noEmit 2>&1 \
  | grep -E "(my-file-pattern)"; echo "TSC_EXIT:${PIPESTATUS[0]}"
```

Or store errors explicitly:

```bash
TSC_OUTPUT=$(node_modules/typescript/bin/tsc --noEmit 2>&1)
TSC_EXIT=$?
echo "$TSC_OUTPUT" | grep -E "(settings-keys|settings-store|preferences|SettingsModal)"
echo "TSC_EXIT:$TSC_EXIT"
```

The second approach (storing output in a variable) is the most reliable since `$?` is captured before the pipe.

---

## Step 4 — Interpret Results

| Condition | Meaning |
|-----------|---------|
| `TSC_EXIT:0`, no grep output | ✅ Full project clean, your files are fine |
| `TSC_EXIT:1`, no grep output | ✅ Your files are fine; pre-existing errors elsewhere (acceptable) |
| `TSC_EXIT:1`, grep output present | ❌ Your files have errors — fix them |
| `TSC_EXIT:2` | ❌ tsc configuration error (bad tsconfig, missing typescript package) |

---

## Fallback: Verify TypeScript Is Installed

If `node_modules/typescript/bin/tsc` does not exist:

```bash
# Check if typescript is installed
ls node_modules/typescript/bin/tsc 2>/dev/null || echo "TypeScript not installed"

# Install if needed
npm install --save-dev typescript
# or
yarn add --dev typescript
```

---

## Full Example

```bash
# Store full tsc output and exit code
TSC_OUTPUT=$(node_modules/typescript/bin/tsc --noEmit 2>&1)
TSC_EXIT=$?

# Show only errors in newly-created files
NEW_FILE_ERRORS=$(echo "$TSC_OUTPUT" | grep -E "(settings-keys|settings-store|preferences|SettingsModal)")

if [ -z "$NEW_FILE_ERRORS" ]; then
  echo "✅ No TypeScript errors in new files (TSC_EXIT:$TSC_EXIT)"
else
  echo "❌ TypeScript errors in new files:"
  echo "$NEW_FILE_ERRORS"
  exit 1
fi
```

---

## Notes

- This technique is safe for CI and local environments alike.
- The direct `node_modules/typescript/bin/tsc` path works on macOS, Linux, and Windows (via `node node_modules/typescript/bin/tsc` on Windows CMD).
- On Windows, replace the bash script with: `node node_modules/typescript/bin/tsc --noEmit`
- Always prefer `--noEmit` for validation-only checks to avoid side effects.