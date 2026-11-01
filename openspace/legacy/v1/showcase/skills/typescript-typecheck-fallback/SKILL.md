---
name: typescript-typecheck-fallback
description: Reliably run TypeScript type-checking (npx tsc --noEmit) by using shell_agent as a fallback when run_shell fails due to environment or timeout issues.
---

# TypeScript Type-Check with shell_agent Fallback

When verifying TypeScript compilation as part of a build or validation workflow, `run_shell` may fail with unknown errors due to shell environment issues, missing PATH entries, timeout constraints, or other transient problems. This skill describes a two-step pattern: attempt the check with `run_shell` first, then fall back to `shell_agent` if it fails.

---

## When to Use

- After creating or modifying TypeScript source files and you need to confirm there are no type errors.
- As part of a CI-like verification step inside an autonomous workflow.
- Any time `run_shell` returns an unexpected or unclear error for a `tsc` invocation.

---

## Step-by-Step Pattern

### Step 1 — Attempt with `run_shell` (fast path)

Try the direct command first. This is fast and gives immediate output when the environment is well-configured.

```
run_shell: npx tsc --noEmit
```

**Evaluate the result:**
- ✅ **Exit code 0, no errors** → Type-check passed. Done.
- ❌ **Exit code non-zero with TypeScript diagnostics** → Real type errors exist; surface them and fix.
- ⚠️ **Unknown error, timeout, or no useful output** → Proceed to Step 2.

---

### Step 2 — Fall back to `shell_agent` (reliable path)

When `run_shell` fails in an ambiguous or environmental way, delegate to `shell_agent` with a natural-language task. `shell_agent` can autonomously:
- Locate the correct `npx`/`tsc` binary
- Retry on transient failures
- Navigate PATH or permission issues
- Return a structured summary of any type errors

**Task string to pass to `shell_agent`:**

```
Run `npx tsc --noEmit` in the project root and report all TypeScript type errors found. If there are no errors, confirm the compilation succeeded. If tsc is not available, attempt to install TypeScript locally with `npm install --save-dev typescript` first.
```

**Evaluate the result:**
- ✅ **"No errors found" / exit 0** → Type-check passed.
- ❌ **Type errors listed** → Collect and fix the reported diagnostics, then re-run.
- ⚠️ **`tsconfig.json` missing** → Ensure the file exists in the project root before re-running.

---

## Decision Flowchart

```
run_shell("npx tsc --noEmit")
        │
        ├─ success (exit 0) ──────────────────► ✅ PASS
        │
        ├─ type errors reported ──────────────► ❌ Fix errors, retry
        │
        └─ unknown/env error ─────────────────► shell_agent(natural-language task)
                                                        │
                                                        ├─ success ───► ✅ PASS
                                                        └─ errors  ───► ❌ Fix & retry
```

---

## Example Implementation

```python
# Pseudo-code for an autonomous agent loop

result = run_shell("npx tsc --noEmit", timeout=60)

if result.exit_code == 0:
    print("TypeScript check passed.")

elif result.exit_code != 0 and result.stderr contains "TS\d+":
    # Real TypeScript diagnostics — surface and fix them
    raise TypeScriptError(result.stderr)

else:
    # Ambiguous failure — delegate to shell_agent
    shell_agent(
        task="Run `npx tsc --noEmit` in the project root and report all TypeScript "
             "type errors found. If there are no errors, confirm compilation succeeded. "
             "If tsc is not available, install it first with `npm install --save-dev typescript`."
    )
```

---

## Tips

- **Always have a `tsconfig.json`**: `tsc --noEmit` requires a valid config. If none exists, generate one first with `npx tsc --init`.
- **Set a reasonable timeout**: TypeScript compilation on large projects can take 30–120 seconds. Give `run_shell` at least `timeout=90`.
- **Scope to changed files when possible**: Use `--project tsconfig.json` or a path glob to limit what tsc checks if only a subset of files changed.
- **Prefer `shell_agent` for first-time setups**: If the project environment is freshly created and dependencies may not be fully installed, start with `shell_agent` directly to avoid a predictable `run_shell` failure.

---

## Generalizing Beyond TypeScript

This same fallback pattern applies to any build/lint/type-check command that may be sensitive to shell environment:

| Tool | Primary `run_shell` command | `shell_agent` fallback task |
|---|---|---|
| TypeScript | `npx tsc --noEmit` | "Run npx tsc --noEmit and report errors" |
| ESLint | `npx eslint src/` | "Run npx eslint on the src directory and report lint errors" |
| Python mypy | `python -m mypy src/` | "Run mypy on the src directory and report type errors" |
| Go build | `go build ./...` | "Run go build and report compilation errors" |

The core principle: **`run_shell` for speed; `shell_agent` for resilience.**