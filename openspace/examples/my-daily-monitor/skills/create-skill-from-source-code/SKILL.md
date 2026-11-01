---
name: create-skill-from-source-code
description: Analyze existing source code to identify key patterns and generate comprehensive educational skill documentation with proper YAML frontmatter, architectural analysis, code templates, and usage examples.
---

# Create Skill from Source Code

This skill guides you through the process of analyzing an existing source code implementation and transforming it into comprehensive educational documentation in the form of a skill file.

## Overview

When you have a reference implementation and need to create teaching materials or reusable documentation from it, follow this systematic workflow to extract patterns, analyze architecture, and generate well-structured educational content.

## Prerequisites

- Access to the source code file(s) you want to document
- Understanding of the target audience for the skill
- Knowledge of the programming language/framework used

## Workflow Steps

### 1. Read and Parse the Source Code

First, read the complete source file to understand its full scope:

```bash
# Read the source file
cat /path/to/source/file.ext
```

Or use file reading tools to access the content programmatically.

### 2. Identify Key Architectural Patterns

Analyze the code to identify:

- **Core architectural patterns**: What design patterns are used? (e.g., Observer, Factory, MVC)
- **Key implementation techniques**: How are specific challenges solved?
- **Important data structures**: What structures enable the functionality?
- **Critical algorithms**: What are the main algorithmic approaches?
- **Integration points**: How does the code interact with external systems?

Take notes on 3-5 major patterns that define the implementation.

### 3. Create the Skill Directory Structure

```bash
# Create skill directory
mkdir -p skills/skill-name

# If you need auxiliary files (examples, scripts, etc.)
mkdir -p skills/skill-name/examples
mkdir -p skills/skill-name/templates
```

### 4. Generate Comprehensive SKILL.md

Create a SKILL.md file with the following structure:

#### Required YAML Frontmatter

```yaml
---
name: descriptive-skill-name
description: Brief one-sentence description of what the skill teaches
---
```

**Frontmatter rules:**
- Must be enclosed in `---` fences
- `name`: lowercase, use hyphens for spaces
- `description`: concise but informative (1-2 sentences max)

#### Document Body Structure

Organize the content into clear sections:

```markdown
# [Skill Title]

## Overview
Brief introduction to what pattern/technique is being taught.

## Key Patterns Identified

### Pattern 1: [Name]
**Purpose**: What problem does it solve?
**Implementation**: How is it implemented in the reference code?
**Key Code Elements**: 
- Relevant classes/functions
- Important variables/structures

### Pattern 2: [Name]
[Same structure as Pattern 1]

[Continue for 3-5 main patterns]

## Complete Code Template

Provide a minimal but complete implementation:

\`\`\`[language]
// Simplified, well-commented version of the key code
// Include only essential elements
// Add explanatory comments
\`\`\`

## Usage Examples

### Example 1: Basic Usage
\`\`\`[language]
// Practical example showing how to use the pattern
\`\`\`

### Example 2: Advanced Usage
\`\`\`[language]
// More complex scenario
\`\`\`

## Best Practices

- List key recommendations
- Common pitfalls to avoid
- Performance considerations
- Testing strategies

## When to Use This Pattern

Explain scenarios where this pattern is appropriate and where it might not be the best choice.

## Related Patterns

Reference related techniques or alternative approaches.

## References

- Link to original source (if applicable)
- Related documentation
- Further reading
```

### 5. Verify File Creation and Frontmatter

After creating the skill file, verify:

```bash
# Check file exists
ls -la skills/skill-name/SKILL.md

# Verify frontmatter format
head -n 5 skills/skill-name/SKILL.md

# Should show:
# ---
# name: skill-name
# description: Description here
# ---
```

Ensure:
- File starts with `---` on line 1
- YAML contains at least `name` and `description` fields
- Frontmatter closes with `---`
- Content follows immediately after

### 6. Add Auxiliary Files (Optional)

If the pattern benefits from additional resources:

**Example script** (`examples/demo.sh`):
```bash
#!/bin/bash
# Demonstration script showing the pattern in action
```

**Template file** (`templates/starter.template`):
```
# Ready-to-use template that users can copy
```

**Configuration example** (`examples/config.example`):
```
# Sample configuration demonstrating best practices
```

## Tips for Creating Effective Skills

### Make It Generalizable

- **Abstract away** specific variable names, file paths, or domain-specific details
- **Focus on the pattern**, not the specific implementation
- **Use placeholders** like `[your-project]`, `[input-file]` for context-specific values

### Keep It Actionable

- Use imperative language: "Create...", "Implement...", "Configure..."
- Provide concrete code examples
- Include expected outputs or results
- Add verification steps

### Structure for Clarity

- Use hierarchical headings (##, ###, ####)
- Break complex steps into numbered sub-steps
- Use code blocks with appropriate syntax highlighting
- Add tables or lists for comparing options

### Balance Detail and Brevity

- Be comprehensive but concise
- Focus on the "what" and "why", not just the "how"
- Link to external resources for deep dives
- Include only code essential to understanding the pattern

## Example: Transforming a Virtual List Component

Given a `VirtualList.ts` source file implementing virtual scrolling:

1. **Read**: Load the complete TypeScript source
2. **Identify patterns**: 
   - Chunk-based rendering for performance
   - Scroll event listeners for viewport tracking
   - DOM manipulation for dynamic updates
   - State management for visible items
3. **Create structure**: `skills/virtual-scrolling-pattern/`
4. **Generate SKILL.md**:
   - Frontmatter with name: `virtual-scrolling-pattern`
   - Sections explaining each of the 4 patterns
   - Simplified code template showing core logic
   - Examples of basic and advanced usage
5. **Verify**: Check file format and frontmatter validity

## Common Pitfalls to Avoid

- **Too specific**: Don't tie the skill to one particular codebase
- **Missing frontmatter**: Always include valid YAML frontmatter
- **No examples**: Abstract patterns need concrete examples
- **Incomplete templates**: Code templates should be runnable or near-runnable
- **Poor organization**: Use clear sections with descriptive headings

## Validation Checklist

Before finalizing the skill:

- [ ] YAML frontmatter is present and valid
- [ ] Name is lowercase with hyphens
- [ ] Description is concise (1-2 sentences)
- [ ] Key patterns are clearly identified (3-5 patterns)
- [ ] Code examples are provided and well-commented
- [ ] Usage examples demonstrate practical application
- [ ] Content is generalized beyond the specific source
- [ ] File structure follows conventions
- [ ] No task-specific details remain
- [ ] Instructions are actionable and clear