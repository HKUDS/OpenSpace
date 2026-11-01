---
name: typescript-import-audit
description: Systematic workflow for auditing TypeScript codebases for import hygiene issues (value vs. type imports, duplicates), missing base class extensions, and incomplete barrel file re-exports, with actionable fix patterns.
---

# TypeScript Import & Structure Audit

A step-by-step workflow for auditing TypeScript projects for common structural and import hygiene problems, then applying targeted fixes.

---

## Overview

This skill covers four audit checks and their corresponding fixes:

| # | Issue | Fix |
|---|-------|-----|
| 1 | Value imports used only as type annotations | Split with `import type` |
| 2 | Duplicate imports from the same module | Consolidate into one import statement |
| 3 | Classes missing expected base class extension | Add `extends BaseClass` |
| 4 | Missing re-exports in barrel (`index.ts`) files | Add the missing `export` lines |

---

## Step 1 — Read All Relevant Files in Parallel

Before making any changes, read all files that need to be audited in a single parallel batch. This avoids slow sequential reads.

```
Read in parallel:
  - src/index.ts           (barrel file)
  - src/**/*.ts            (all source files)
  - Any explicitly named files from the task spec
```

Look for:
- All `import` statements at the top of each file
- Class declarations (`class Foo`)
- Export lists in barrel files

---

## Step 2 — Audit Check 1: Value Imports Used Only as Types

### Detection

For every `import { A, B } from 'module'` statement, check whether each
imported name is used **only** in type positions (`: TypeName`, `as TypeName`,
`<TypeName>` generics, `implements TypeName`). If a name never appears in a
value position (instantiation, function call, assignment), it should be a
type-only import.

```typescript
// ❌ Bad — Foo is only used as a type annotation
import { Foo, bar } from './module';
const x: Foo = bar();

// ✅ Good — split into value import + type import
import { bar } from './module';
import type { Foo } from './module';
```

### Fix Pattern

1. Identify all names in the import that are type-only.
2. Remove them from the value import (delete the value import entirely if all names are type-only).
3. Add (or merge into an existing) `import type { ... } from 'module'` statement.

---

## Step 3 — Audit Check 2: Duplicate Imports from the Same Module

### Detection

Scan for two or more `import` statements that reference the **same module path**:

```typescript
// ❌ Bad — two imports from the same source
import { ComponentA } from './components';
import { ComponentB } from './components';
```

### Fix Pattern

Merge all named imports from the same module into a single statement:

```typescript
// ✅ Good
import { ComponentA, ComponentB } from './components';
```

If there is a mix of value and type imports from the same module, keep them as two separate lines (`import { ... }` and `import type { ... }`), but ensure there is at most one of each kind per module path.

---

## Step 4 — Audit Check 3: Classes That Should Extend a Base Class

### Detection

Look for class declarations that:
- Implement an interface that implies a base class, OR
- Are described in comments / documentation as extending something, OR
- Contain methods/properties that are defined on a known base class

```typescript
// ❌ Missing base class
class MyPanel implements PanelInterface {
  render() { ... }
}

// ✅ Correct
class MyPanel extends BasePanel implements PanelInterface {
  render() { ... }
}
```

### Fix Pattern

Add `extends BaseClass` between the class name and any `implements` clause:

```typescript
class ClassName extends BaseClass implements InterfaceName { ... }
```

Ensure the base class is imported if it is not already.

---

## Step 5 — Audit Check 4: Missing Re-exports in Barrel Files

### Detection

A barrel file (`index.ts`) should re-export everything that is part of the
public API. Common gaps:

- A new file was added but never added to `index.ts`
- A named export inside a file is not forwarded by the barrel

```typescript
// src/components/MyWidget.ts
export class MyWidget { ... }
export type MyWidgetProps = { ... }

// src/components/index.ts — ❌ missing MyWidget
export { SomeOtherThing } from './SomeOtherThing';
```

### Fix Pattern

Add the missing re-export line:

```typescript
// src/components/index.ts — ✅ fixed
export { SomeOtherThing } from './SomeOtherThing';
export { MyWidget } from './MyWidget';
export type { MyWidgetProps } from './MyWidget';
```

Use `export type { ... }` for type-only exports to maintain strict type/value separation.

---

## Step 6 — Apply Fixes and Verify

### Order of operations

1. Fix **duplicate imports** first (consolidation may surface type-only candidates).
2. Fix **value vs. type imports** (split imports).
3. Fix **missing base class** extensions.
4. Fix **barrel re-exports** last (they depend on the other files being correct).

### Verification checklist

After edits, confirm:

- [ ] No two `import` statements reference the same module path (same kind).
- [ ] All names used only as types are in `import type { ... }` statements.
- [ ] Every class that should extend a base class does so.
- [ ] Every public export in every source file appears in the barrel `index.ts`.
- [ ] The project compiles without errors (`tsc --noEmit`).

---

## Quick Reference: Import Patterns

```typescript
// Value import
import { foo, bar } from './module';

// Type-only import
import type { Foo, Bar } from './module';

// Mixed (value + type from same module — two lines)
import { foo } from './module';
import type { Foo } from './module';

// Re-export value
export { foo } from './module';

// Re-export type
export type { Foo } from './module';

// Re-export everything
export * from './module';
export type * from './module';  // TypeScript 5.0+
```

---

## Notes

- Always prefer **explicit named exports** over `export *` in barrel files for better tree-shaking and discoverability.
- If the project uses `verbatimModuleSyntax` or `isolatedModules` in `tsconfig.json`, `import type` is **required** for type-only imports — the compiler will error otherwise.
- Run `tsc --noEmit` after each fix category to catch regressions early.