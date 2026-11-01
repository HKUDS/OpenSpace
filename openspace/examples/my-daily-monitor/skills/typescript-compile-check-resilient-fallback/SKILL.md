---
name: typescript-compile-check-resilient-fallback
description: A resilient workflow for running TypeScript compile checks (tsc --noEmit) that falls back gracefully when npx or direct binary invocations fail, and distinguishes pre-existing errors from newly introduced ones.
---

# TypeScript Compile-Check Resilient Fallback

Use this workflow whenever you need to verify that TypeScript files compile
cleanly after creating or modifying them, especially in environments where
`npx` or direct `./node_modules/.bin/tsc` invocations may return unexpected
errors.

---

## Step 1 — Attempt Primary Invocation

Run the compile check with `run_shell` using the standard approach:

```bash
npx tsc --noEmit
```

or, if you already know the project root:

```bash
./node_modules/.bin/tsc --noEmit
```

**Evaluate the result:**
- If it exits cleanly (exit code 0, no unexpected output) → done.
- If it exits with **TypeScript diagnostic errors** → proceed to Step 3.
- If it exits with an **unknown / tool-level error** (e.g., `npx` not found,
  permission denied, spawn error, unclear non-zero exit unrelated to TS
  diagnostics) → proceed to Step 2.

---

## Step 2 — Fallback via `shell_agent`

When the primary invocation fails with an unknown error, delegate to
`shell_agent` so it can resolve environment issues automatically:

> Task: "From the project directory `<PROJECT_DIR>`, run
> `node node_modules/typescript/bin/tsc --noEmit` and report all
> TypeScript diagnostic errors."

Example `shell_agent` task string:

```
From /path/to/project, run:
  node node_modules/typescript/bin/tsc --noEmit
Capture the full stdout/stderr output and return it.
```

`shell_agent` will handle PATH issues, working-directory changes, and
retry on transient failures automatically.

---

## Step 3 — Filter Pre-Existing Errors

TypeScript projects frequently have pre-existing errors in files you did
**not** touch. To avoid false regressions:

1. **Identify the baseline error count** for the project (e.g., determined
   at task start or documented in task context). A common baseline is
   **11 pre-existing errors** in unrelated files.
2. **Collect the full diagnostic list** from tsc output.
3. **Filter** to keep only errors whose file path matches a file you
   created or modified in this task.
4. **Evaluate:**
   - Zero errors in your new/modified files → ✅ no regression introduced.
   - One or more errors in your new/modified files → ❌ fix them before
     proceeding.

### Filtering heuristic (pseudocode)

```
all_errors   = parse_tsc_output(tsc_stdout)
my_files     = [list of files created/modified in this task]
new_errors   = [e for e in all_errors if e.file in my_files]

if len(new_errors) == 0:
    print("✅ No new TypeScript errors introduced.")
else:
    for err in new_errors:
        print(f"❌ {err.file}:{err.line} — {err.message}")
    raise Exception("Fix TypeScript errors before continuing.")
```

---

## Step 4 — Fix and Re-Run

If new errors are found:
1. Fix the errors in your file(s).
2. Repeat from **Step 1**.

---

## Decision Tree Summary

```
run_shell: npx tsc --noEmit
        │
        ├─ clean exit ──────────────────────────────► ✅ Done
        │
        ├─ TS diagnostic errors ────────────────────► Step 3 (filter)
        │
        └─ unknown/tool error
                │
                └─ shell_agent: node .../tsc --noEmit
                        │
                        ├─ TS diagnostic errors ────► Step 3 (filter)
                        └─ clean exit ──────────────► ✅ Done
```

---

## Notes

- Always run tsc from the **project root** (where `tsconfig.json` lives).
- The `node node_modules/typescript/bin/tsc` invocation is the most portable
  fallback because it bypasses `npx`, shell PATH lookups, and binary
  permission issues entirely.
- When the baseline error count is unknown, run tsc **before** making any
  changes and record the count; that becomes your baseline.
- Only report/act on errors in files **you own**; never attempt to fix
  pre-existing errors in third-party or unrelated project files unless
  explicitly asked.