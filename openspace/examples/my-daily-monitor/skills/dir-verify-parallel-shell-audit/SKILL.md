---
name: dir-verify-parallel-shell-audit
description: Diagnose and resolve file access issues by verifying directory structure, reading multiple files in parallel batches, running combined shell property checks (head/wc-l) in the same iteration as directory listing, and validating TypeScript import patterns — all with built-in guidance to minimize total iteration count.
---

# Verify Directory Structure — Parallel Audit with Shell Property Checks

This skill provides a systematic approach to diagnosing file access issues and auditing source trees efficiently. It extends the parallel batch reading approach with an **integrated shell property check phase** that combines `head -N` and `wc -l` into a single `run_shell` call, co-scheduled with directory listing in Phase 1. This eliminates a common extra iteration that would otherwise be required to check file properties such as line counts and leading content (e.g., CSS/config file headers).

---

## Steps

### Phase 1 — Directory Verification + Shell Property Checks (Combined)

> **Key optimization**: Fire `list_dir` calls **and** `run_shell` property checks in the same iteration. This avoids a dedicated iteration later just to learn "how many lines does this file have?" or "what is the first line of this CSS file?".

1. **Identify Target Paths**: Collect every file or directory path involved in the operation. Prefer absolute paths to eliminate ambiguity. At this stage, also note any files whose properties (line count, header content) you already know you will need — typically CSS, config, or data files.

2. **Issue Parallel `list_dir` + `run_shell` Calls Simultaneously**:

   - Call `list_dir` on each relevant directory.
   - In the **same iteration**, issue one or more `run_shell` calls that bundle `head` and `wc -l` for known target files using a single compound shell command.

   ```
   Iteration 1 (parallel):
     list_dir(src/components)
     list_dir(src/styles)
     run_shell("head -5 src/styles/main.css && echo '---' && wc -l src/styles/main.css")
     run_shell("head -3 vite.config.ts && echo '---' && wc -l src/main.ts")
   ```

   **Compound shell pattern** (combine head + wc + any other checks for a file):
   ```bash
   # Single run_shell call — multiple files, multiple checks, zero extra iterations
   head -10 path/to/file.css && echo "LINE_COUNT:" && wc -l path/to/file.css && \
   echo "---" && head -5 path/to/config.ts && echo "LINE_COUNT:" && wc -l path/to/config.ts
   ```

3. **Verify Existence & Permissions**: Confirm directories exist and that the current user has read (and, if needed, write/execute) permissions. If a directory is missing, create it before proceeding (`mkdir -p`). If permissions are insufficient, adjust them before retrying.

4. **Build a File Manifest**: From the `list_dir` output, compile the complete list of files to be audited. Group them logically (e.g., by subdirectory or file type) so they can be read in parallel batches in Phase 2.

5. **Record Shell Check Results**: Parse and store the `head`/`wc -l` output alongside the manifest. This data is immediately available for the final audit report without any additional iterations.

---

### Phase 2 — Parallel Batch File Reading

> **Why this matters**: Reading files sequentially costs one iteration per file. Reading them in parallel batches collapses N files into ⌈N/batch_size⌉ iterations — a significant speedup when auditing dozens of files.

6. **Determine Batch Size**: Default to reading **5–10 files per batch**. Reduce the batch size if individual files are very large; increase it for small config/index files. Files whose content was partially previewed via `head` in Phase 1 should still be fully read here if their complete content is needed.

7. **Issue Parallel `read_file` Calls**: Within each batch, invoke `read_file` for all files simultaneously (a single iteration). Do not wait for one file before requesting the next within the same batch.

   ```
   Batch 1 (parallel): read_file(src/components/A.tsx)
                        read_file(src/components/B.tsx)
                        read_file(src/components/C.tsx)
                        read_file(src/components/D.tsx)
                        read_file(src/components/E.tsx)

   Batch 2 (parallel): read_file(src/components/F.tsx)
                        ...
   ```

8. **Collect & Triage Results**: After each batch completes, scan results for errors (file-not-found, permission denied) and flag them immediately. Do not abort the remaining batches; continue and aggregate all findings.

9. **Re-verify Missing Files**: For any file reported as missing, re-run `list_dir` on its parent directory to confirm whether the file truly does not exist or whether the path was wrong. Correct the path and retry the single file if necessary.

---

### Phase 3 — TypeScript Import Validation

> This phase targets a common class of structural bugs found when auditing TypeScript source trees.

10. **Identify Import Statements**: For every `.ts` / `.tsx` file read in Phase 2, extract all `import` lines.

11. **Classify Import Kind — Value vs. Type**:

    | Pattern | Correct usage |
    |---|---|
    | `import Foo from '...'` | Runtime value (class, function, object) |
    | `import { Foo } from '...'` | Runtime value export |
    | `import type Foo from '...'` | Type-only import (erased at compile time) |
    | `import type { Foo } from '...'` | Type-only named import |
    | `import { type Foo } from '...'` | Inline type modifier (TS 4.5+) |

12. **Flag Value Imports Used Only as Types**: If an identifier imported without `type` is used **only** in type positions (`: Foo`, `as Foo`, `implements Foo`, generic parameters), it should be converted to `import type`. This prevents accidental runtime dependencies and satisfies `verbatimModuleSyntax` / `isolatedModules` compiler flags.

    ```typescript
    // ❌ Before — value import used only as a type
    import { UserProfile } from './types';
    const handler = (u: UserProfile) => { ... };

    // ✅ After — type-only import
    import type { UserProfile } from './types';
    const handler = (u: UserProfile) => { ... };
    ```

13. **Flag Duplicate Imports**: Detect cases where the same module is imported more than once in a file (often from copy-paste). Merge them into a single import statement.

    ```typescript
    // ❌ Duplicate
    import { A } from './utils';
    import { B } from './utils';

    // ✅ Merged
    import { A, B } from './utils';
    ```

14. **Verify Barrel / Index Exports**: When a directory contains an `index.ts` (barrel file), confirm that every component or module expected to be publicly accessible is re-exported. Cross-reference against the file manifest from Phase 1.

15. **Apply Fixes & Re-audit**: Apply all import corrections as a single write pass per file. After writing, re-read the affected files (parallel batch) to confirm the fixes took effect.

---

### Phase 4 — Final Validation & Report

16. **Retry the Original Operation**: With the directory structure confirmed and import issues resolved, retry the operation that originally failed.

17. **Log an Audit Report**: Produce a concise summary covering:
    - Total files enumerated and read
    - Number of batches used (highlight the iteration savings vs. sequential)
    - Shell property check results (line counts, file headers) gathered in Phase 1 at zero extra iteration cost
    - Import issues found and fixed (value→type conversions, duplicate merges, missing barrel exports)
    - Any remaining unresolved issues with recommended next steps

---

## Quick Reference: Iteration Collapse Patterns

```
# Sequential (slow) — N+K iterations for N files + K property checks
list_dir(dir)         → wait
run_shell(wc -l ...)  → wait  ← extra iteration that can be eliminated
read_file(file1)      → wait
read_file(file2)      → wait
...

# Optimized (fast) — 1 + ⌈N/B⌉ iterations
[list_dir(dir), run_shell("head -5 f1 && wc -l f1 && head -5 f2 && wc -l f2")]  ← 1 iteration (Phase 1)
[read_file(file1), read_file(file2), ..., read_file(fileB)]                       ← 1 iteration (Phase 2, batch 1)
[read_file(fileB+1), ...]                                                          ← 1 iteration (Phase 2, batch 2)
```

### Compound Shell Command Templates

```bash
# Check header + line count for a single file
head -10 /path/to/file.css && echo "LINES:" && wc -l /path/to/file.css

# Check multiple files in one run_shell call
head -5 /path/to/a.css && echo "LINES_A:" && wc -l /path/to/a.css && \
echo "===" && \
head -5 /path/to/b.ts  && echo "LINES_B:" && wc -l /path/to/b.ts

# Verify a file starts with expected content (e.g., CSS theme comment)
head -1 /path/to/styles.css | grep -q "Theme Colors" && echo "OK" || echo "MISSING HEADER"

# Count files matching a pattern in a directory
ls src/components/*.tsx | wc -l
```

---

## Best Practices

- **Co-schedule shell checks with `list_dir`**: Any `wc -l` or `head -N` call you know you'll need belongs in the same iteration as your initial `list_dir` calls — never in a dedicated follow-up iteration.
- **Bundle multiple shell checks into one `run_shell`**: Use `&&` or `;` to chain head/wc/grep commands for multiple files into a single `run_shell` invocation. Each `run_shell` call costs one tool-call slot; batching them saves slots.
- **Always enumerate before reading**: `list_dir` first, then batch-read. Never guess file names.
- **Use absolute paths** throughout to avoid working-directory ambiguity.
- **Batch by locality**: Group files from the same directory in the same batch to keep context coherent.
- **Type imports are zero-cost at runtime**: Prefer `import type` for any symbol used exclusively as a TypeScript type. This is especially important in projects with `isolatedModules: true`.
- **Barrel files are contracts**: Treat `index.ts` exports as the public API of a module. Missing exports are bugs even if the underlying file is correct.
- **Log intermediate findings**: If issues persist after Phase 3, dump the full audit log for offline debugging.

---

## Common Fix Categories (Checklist)

- [ ] Directory missing → create with `mkdir -p`
- [ ] Permissions insufficient → `chmod`/`chown` or escalate
- [ ] Path typo → re-enumerate with `list_dir`, correct path
- [ ] File has wrong line count → inspect with `wc -l` (already gathered in Phase 1 at no extra cost)
- [ ] File has wrong header content → inspect with `head -N` (already gathered in Phase 1 at no extra cost)
- [ ] Value import used as type → add `import type`
- [ ] Duplicate imports → merge into single statement
- [ ] Missing barrel export → add re-export to `index.ts`
- [ ] Circular import → restructure module boundaries
