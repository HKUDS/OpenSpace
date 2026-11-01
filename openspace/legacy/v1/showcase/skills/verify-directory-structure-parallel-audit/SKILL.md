---
name: verify-directory-structure-parallel-audit
description: Diagnose and resolve file access issues by verifying directory structure, then reading multiple files in parallel batches for efficient auditing, with built-in guidance for validating TypeScript import patterns.
---

# Verify Directory Structure — Parallel Audit

This skill provides a systematic approach to diagnosing file access issues and auditing source trees efficiently. It extends basic directory verification with **parallel batch file reading** (dramatically reducing iteration count when many files must be inspected) and a dedicated **TypeScript import validation** phase that catches a common class of structural bugs.

---

## Steps

### Phase 1 — Directory Verification

1. **Identify Target Paths**: Collect every file or directory path involved in the operation. Prefer absolute paths to eliminate ambiguity.

2. **Enumerate with `list_dir`**: Call `list_dir` on each relevant directory. Record all file names, sizes, and modification dates. This is your ground truth before touching any file.

3. **Verify Existence & Permissions**: Confirm the directory exists and that the current user has read (and, if needed, write/execute) permissions. If a directory is missing, create it before proceeding. If permissions are insufficient, adjust them before retrying.

4. **Build a File Manifest**: From the `list_dir` output, compile the complete list of files to be audited. Group them logically (e.g., by subdirectory or file type) so they can be read in parallel batches.

---

### Phase 2 — Parallel Batch File Reading

> **Why this matters**: Reading files sequentially costs one iteration per file. Reading them in parallel batches collapses N files into ⌈N/batch_size⌉ iterations — a significant speedup when auditing dozens of files.

5. **Determine Batch Size**: Default to reading **5–10 files per batch**. Reduce the batch size if individual files are very large; increase it for small config/index files.

6. **Issue Parallel `read_file` Calls**: Within each batch, invoke `read_file` for all files simultaneously (a single iteration). Do not wait for one file before requesting the next within the same batch.

   ```
   Batch 1 (parallel): read_file(src/components/A.tsx)
                        read_file(src/components/B.tsx)
                        read_file(src/components/C.tsx)
                        read_file(src/components/D.tsx)
                        read_file(src/components/E.tsx)

   Batch 2 (parallel): read_file(src/components/F.tsx)
                        ...
   ```

7. **Collect & Triage Results**: After each batch completes, scan results for errors (file-not-found, permission denied) and flag them immediately. Do not abort the remaining batches; continue and aggregate all findings.

8. **Re-verify Missing Files**: For any file reported as missing, re-run `list_dir` on its parent directory to confirm whether the file truly does not exist or whether the path was wrong. Correct the path and retry the single file if necessary.

---

### Phase 3 — TypeScript Import Validation

> This phase targets a common class of structural bugs found when auditing TypeScript source trees.

9. **Identify Import Statements**: For every `.ts` / `.tsx` file read in Phase 2, extract all `import` lines.

10. **Classify Import Kind — Value vs. Type**:

    | Pattern | Correct usage |
    |---|---|
    | `import Foo from '...'` | Runtime value (class, function, object) |
    | `import { Foo } from '...'` | Runtime value export |
    | `import type Foo from '...'` | Type-only import (erased at compile time) |
    | `import type { Foo } from '...'` | Type-only named import |
    | `import { type Foo } from '...'` | Inline type modifier (TS 4.5+) |

11. **Flag Value Imports Used Only as Types**: If an identifier imported without `type` is used **only** in type positions (`: Foo`, `as Foo`, `implements Foo`, generic parameters), it should be converted to `import type`. This prevents accidental runtime dependencies and satisfies `verbatimModuleSyntax` / `isolatedModules` compiler flags.

    ```typescript
    // ❌ Before — value import used only as a type
    import { UserProfile } from './types';
    const handler = (u: UserProfile) => { ... };

    // ✅ After — type-only import
    import type { UserProfile } from './types';
    const handler = (u: UserProfile) => { ... };
    ```

12. **Flag Duplicate Imports**: Detect cases where the same module is imported more than once in a file (often from copy-paste). Merge them into a single import statement.

    ```typescript
    // ❌ Duplicate
    import { A } from './utils';
    import { B } from './utils';

    // ✅ Merged
    import { A, B } from './utils';
    ```

13. **Verify Barrel / Index Exports**: When a directory contains an `index.ts` (barrel file), confirm that every component or module expected to be publicly accessible is re-exported. Cross-reference against the file manifest from Phase 1.

14. **Apply Fixes & Re-audit**: Apply all import corrections as a single write pass per file. After writing, re-read the affected files (parallel batch) to confirm the fixes took effect.

---

### Phase 4 — Final Validation & Report

15. **Retry the Original Operation**: With the directory structure confirmed and import issues resolved, retry the operation that originally failed.

16. **Log an Audit Report**: Produce a concise summary covering:
    - Total files enumerated and read
    - Number of batches used (highlight the iteration savings vs. sequential)
    - Import issues found and fixed (value→type conversions, duplicate merges, missing barrel exports)
    - Any remaining unresolved issues with recommended next steps

---

## Quick Reference: Batch Reading Pattern

```
# Sequential (slow) — N iterations for N files
read_file(file1) → wait → read_file(file2) → wait → ...

# Parallel batch (fast) — ⌈N/B⌉ iterations for N files, batch size B
[read_file(file1), read_file(file2), ..., read_file(fileB)]  ← 1 iteration
[read_file(fileB+1), ...]                                     ← 1 iteration
```

---

## Best Practices

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
- [ ] Value import used as type → add `import type`
- [ ] Duplicate imports → merge into single statement
- [ ] Missing barrel export → add re-export to `index.ts`
- [ ] Circular import → restructure module boundaries
