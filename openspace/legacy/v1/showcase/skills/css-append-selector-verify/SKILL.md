---
name: css-append-selector-verify
description: Safely append CSS styles from one file to another, with per-selector grep verification after every append to catch silent truncation that tail or wc-l alone would miss
---

# Safe CSS Append Workflow (with Per-Selector Verification)

This skill provides a systematic approach for safely appending CSS styles from a source file to a destination file. The workflow ensures no content is overwritten, CSS variables are properly adapted, and **every appended selector is individually confirmed present** — so silent heredoc truncation of long lines is caught immediately rather than being masked by a passing line-count check.

## When to Use This Skill

- Migrating styles between projects
- Copying component styles while preserving existing rules
- Adding new CSS rules without duplicating or conflicting with existing styles
- Adapting styles to use a different set of CSS variables

## Workflow Steps

### 1. Read and Analyze Source CSS

First, examine the source CSS file to identify the selectors and rules you want to transfer:

```bash
# Read the source CSS file
cat /path/to/source/styles.css

# Identify specific selectors if needed
grep "selector-pattern" /path/to/source/styles.css
```

**Document:**
- Target selectors (e.g., `.panel-tab`, `.header-nav`)
- CSS variables used (e.g., `--primary-color`, `--spacing-unit`)
- Complete rule sets for each selector

### 2. Read and Check Destination CSS

Examine the destination file to understand existing rules and avoid duplication:

```bash
# Read the destination CSS file
cat /path/to/destination/styles.css

# Check if specific selectors already exist
grep "selector-name" /path/to/destination/styles.css

# Check for existing CSS variables
grep "^[[:space:]]*--" /path/to/destination/styles.css | head -20
```

**Identify:**
- Existing selectors that might conflict
- CSS variable naming conventions in the destination
- Any existing rules for the target selectors

### 3. Identify Missing Styles and Required Adaptations

Compare source and destination to determine:
- Which selectors are completely missing
- Which properties are missing from existing selectors
- How CSS variables need to be mapped (source → destination)

**Example variable mapping:**
```
Source: --panel-bg → Destination: --background-secondary
Source: --text-primary → Destination: --color-text-primary
Source: --border-radius → Keep as-is (if already defined in destination)
```

### 4. Prepare Adapted CSS Content

Create the CSS content to append, adapting variables as needed:

```css
/* Example adapted CSS */
.new-selector {
    background-color: var(--background-secondary);  /* was --panel-bg */
    color: var(--color-text-primary);               /* was --text-primary */
    border-radius: var(--border-radius);            /* unchanged */
    padding: 8px 16px;
}

.existing-selector {
    /* Only add missing properties, not the entire rule */
    new-property: value;
}
```

### 5. Safely Append to Destination

Use append operations (never overwrite) to add the new styles.

**Record the line count BEFORE appending** (use this in step 6):

```bash
BEFORE=$(wc -l < /path/to/destination/styles.css)
```

Then append:

```bash
cat >> /path/to/destination/styles.css << 'EOF'

/* Added styles from source project */
.selector-name {
    property: value;
    variable-property: var(--adapted-variable);
}
EOF
```

**Best practices:**
- Add a comment indicating the source or purpose
- Include a blank line before the new content for readability
- Use `>>` (append) never `>` (overwrite)

### 6. Verify the Changes (Per-Selector Grep + Tail)

Confirm that the styles were added correctly. **Do not rely on `tail` or `wc -l` alone** — a heredoc can silently truncate an overly long line, leaving a selector block incomplete while the line count still increases. The required approach is to grep for *every* selector that was just appended.

#### 6a. Line-count delta

```bash
AFTER=$(wc -l < /path/to/destination/styles.css)
echo "Lines added: $((AFTER - BEFORE))"
```

#### 6b. Per-selector grep — one grep per appended selector

```bash
# Repeat for every selector appended in step 5
grep -n "\.selector-name"      /path/to/destination/styles.css || echo "MISSING: .selector-name"
grep -n "\.another-selector"   /path/to/destination/styles.css || echo "MISSING: .another-selector"
```

If any grep prints "MISSING", the append was incomplete — re-run step 5.

#### 6c. Automation helper for many selectors

If many selectors were appended, use a loop:

```bash
DEST=/path/to/destination/styles.css
MISSING=0
for selector in \
    ".selector-name" \
    ".another-selector" \
    ".yet-another"; do
  if ! grep -q "$selector" "$DEST"; then
    echo "MISSING SELECTOR: $selector"
    MISSING=$((MISSING + 1))
  fi
done

if [ "$MISSING" -gt 0 ]; then
  echo "ERROR: $MISSING selector(s) not found — append may have been truncated."
  exit 1
else
  echo "All selectors verified present."
fi
```

#### 6d. Visual spot-check

```bash
tail -n 30 /path/to/destination/styles.css
```

**Why per-selector grep is required:**
- `wc -l` increments even when a line is truncated — the count passes but content is lost.
- `tail` shows recent lines but only reveals truncation if you happen to notice a cut-off line visually.
- A `grep` for each selector name fails with a non-zero exit code if the selector is absent, making the failure explicit and automatable.

## Complete Example

Here's a full example workflow:

```bash
# 1. Read source CSS and identify target
cat project-a/components.css | grep -A 10 ".button-primary"

# 2. Check destination for conflicts
grep ".button-primary" project-b/main.css
grep "^[[:space:]]*--btn-" project-b/main.css

# 3. Identify that .button-primary is missing, and variables need mapping:
#    --btn-color → --primary-color (exists in destination)
#    --btn-padding → create inline value

# 4. Record line count before append
BEFORE=$(wc -l < project-b/main.css)

# 5. Append adapted styles
cat >> project-b/main.css << 'EOF'

/* Button styles from project-a */
.button-primary {
    background-color: var(--primary-color);
    padding: 10px 20px;
    border-radius: 4px;
    font-weight: 600;
}
EOF

# 6a. Line-count delta
AFTER=$(wc -l < project-b/main.css)
echo "Lines before: $BEFORE  |  Lines after: $AFTER  |  Added: $((AFTER - BEFORE))"

# 6b. Per-selector grep — catches truncation that wc -l and tail miss
grep -n "\.button-primary" project-b/main.css \
  && echo "OK: .button-primary" \
  || echo "MISSING: .button-primary — possible truncation!"

# 6c. Visual spot-check
tail -n 10 project-b/main.css
```

## Safety Checklist

- [ ] Source file read and analyzed
- [ ] Destination file checked for existing rules
- [ ] Missing styles identified
- [ ] CSS variables mapped correctly
- [ ] Append operation used (not overwrite)
- [ ] Every appended selector individually confirmed with grep (not just tail/wc -l)
- [ ] Automation loop (or per-selector greps) returned zero missing selectors
- [ ] No duplicate selectors created
- [ ] All variable references exist in destination

## Common Pitfalls

1. **Overwriting instead of appending**: Always use `>>`, never `>`
2. **Variable mismatch**: Ensure all CSS variables exist in the destination or are properly defined
3. **Duplicate selectors**: Check for existing rules before adding complete selector blocks
4. **Incomplete verification**: Always grep for every appended selector by name — `tail` and `wc -l` do not detect mid-block truncation
5. **Heredoc line-length truncation**: Very long CSS property values or data URIs inside a heredoc can be silently cut. If a line is extremely long, use a temporary file approach instead (see Tips)

## Tips

- Use `wc -l` before and after as a *supplementary* sanity check, but never as the sole verification
- Collect all selector names appended in a shell array at write time, then loop over them in the verification step — this eliminates the risk of forgetting to check a selector
- Keep a mapping document for complex variable transformations
- For very long lines (e.g., embedded SVGs or data URIs), write the CSS to a temp file first and use `cat tmpfile >> dest.css` rather than a heredoc; then grep-verify as normal
- Group related selectors together when appending multiple rules
