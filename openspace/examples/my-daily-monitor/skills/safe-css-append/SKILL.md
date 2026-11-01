---
name: safe-css-append
description: Safely append CSS styles from one file to another by identifying missing rules, adapting CSS variables, and verifying changes
---

# Safe CSS Append Workflow

This skill provides a systematic approach for safely appending CSS styles from a source file to a destination file. The workflow ensures no content is overwritten, CSS variables are properly adapted, and all changes are verified.

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

Use append operations (never overwrite) to add the new styles:

```bash
# Append new CSS rules
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

### 6. Verify the Changes

Confirm that the styles were added correctly:

```bash
# View the last N lines to see appended content
tail -n 20 /path/to/destination/styles.css

# Verify specific selectors were added
grep "new-selector-name" /path/to/destination/styles.css

# Check the complete file if needed
cat /path/to/destination/styles.css | tail -n 50
```

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

# 4. Append adapted styles
cat >> project-b/main.css << 'EOF'

/* Button styles from project-a */
.button-primary {
    background-color: var(--primary-color);
    padding: 10px 20px;
    border-radius: 4px;
    font-weight: 600;
}
EOF

# 5. Verify
tail -n 10 project-b/main.css
grep ".button-primary" project-b/main.css
```

## Safety Checklist

- [ ] Source file read and analyzed
- [ ] Destination file checked for existing rules
- [ ] Missing styles identified
- [ ] CSS variables mapped correctly
- [ ] Append operation used (not overwrite)
- [ ] Changes verified with tail/grep
- [ ] No duplicate selectors created
- [ ] All variable references exist in destination

## Common Pitfalls

1. **Overwriting instead of appending**: Always use `>>`, never `>`
2. **Variable mismatch**: Ensure all CSS variables exist in the destination or are properly defined
3. **Duplicate selectors**: Check for existing rules before adding complete selector blocks
4. **Missing verification**: Always verify changes after appending

## Tips

- Use `wc -l` before and after to confirm lines were added
- Keep a mapping document for complex variable transformations
- Consider using a temporary file to preview changes before appending
- Group related selectors together when appending multiple rules