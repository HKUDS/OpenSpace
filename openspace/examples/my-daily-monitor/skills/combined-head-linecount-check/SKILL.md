---
name: combined-head-linecount-check
description: Verify a file's first N lines and total line count in one shell call using head and wc -l with an echo separator, minimizing read operations and shell iterations.
---

# Combined Head + Line Count Check

When you need to **both** inspect the top of a file and confirm its total line count, collapse the two checks into a single `run_shell` call instead of issuing two separate commands.

## Why

- Each shell call is a round-trip. Two separate calls (`head` then `wc -l`) cost two iterations.
- A combined call costs one iteration and returns both pieces of information atomically.
- When checking multiple files at once, each file still only needs one call (or all files can be parallelized in a single compound command).

## Pattern

```bash
head -N <file> && echo "---" && wc -l <file>
```

The `echo "---"` separator makes the output easy to parse visually: everything above the dashes is content, everything below is the line count.

## Examples

### Single file — inspect first 10 lines and total count

```bash
head -10 src/main.ts && echo "---" && wc -l src/main.ts
```

Sample output:
```
import { createApp } from 'vue'
import App from './App.vue'
...
---
      142 src/main.ts
```

### Multiple files — fully parallelized

When you need to check several files simultaneously, run them all in one shell call using `&&` chains or newline-separated subshells:

```bash
(head -10 src/main.ts     && echo "---" && wc -l src/main.ts)     && echo "===" && \
(head -10 vite.config.ts  && echo "---" && wc -l vite.config.ts)  && echo "===" && \
(head -5  package.json    && echo "---" && wc -l package.json)
```

Or, if order independence is acceptable, run them in parallel background jobs and wait:

```bash
{
  head -10 src/main.ts    && echo "---" && wc -l src/main.ts
} &
{
  head -10 vite.config.ts && echo "---" && wc -l vite.config.ts
} &
wait
```

## When to Apply

| Situation | Recommendation |
|-----------|---------------|
| Verifying a generated/modified file was written correctly | ✅ Use combined call |
| Checking multiple output files after a build step | ✅ Parallelize all in one shell call |
| Only need line count (no content preview required) | Use plain `wc -l` |
| Only need content (no line count required) | Use plain `head -N` |
| File is extremely large and you want a middle section | Use `sed -n` or `awk` instead |

## Integration with Parallel Reads

This pattern compounds well with parallel file reads. In a single iteration you can:

1. Read file contents with `cat` or `head` for multiple files.
2. Simultaneously verify line counts with `wc -l`.
3. Check for specific patterns with `grep -c`.

Example of a 5-file parallel verification in one shell call:

```bash
echo "=== main.ts ===" && head -10 src/main.ts && echo "lines:" && wc -l < src/main.ts && \
echo "=== config ===" && head -5 vite.config.ts && echo "lines:" && wc -l < vite.config.ts && \
echo "=== package ===" && head -5 package.json && echo "lines:" && wc -l < package.json && \
echo "=== index ===" && head -8 index.html && echo "lines:" && wc -l < index.html && \
echo "=== readme ===" && head -5 README.md && echo "lines:" && wc -l < README.md
```

> **Tip:** Using `wc -l < file` (with input redirect) prints only the number, without the filename — cleaner output when the filename is already shown by the `echo` label above it.

## Key Takeaway

> **Never use two shell calls where one will do.** Combining `head -N` and `wc -l` in a single command is the minimal-overhead way to answer both "what does this file contain?" and "is it the right size?" simultaneously.