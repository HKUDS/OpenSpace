---
name: heredoc-file-write
description: Use shell heredoc syntax as a workaround when write_file fails on complex TypeScript/JSX/code files with encoding or escaping issues
---

# Heredoc File Write Workaround

## When to Use This Skill

Use this pattern when:
- `write_file` repeatedly fails with "unknown error" or similar messages
- The target file contains complex code (TypeScript, JSX, JSON with special characters, etc.)
- Standard file writing approaches encounter encoding or escaping issues
- You need a reliable fallback method to create files

## Core Technique

Switch from `write_file` to `run_shell` using heredoc syntax with `cat`. The heredoc approach handles special characters, quotes, and complex content more reliably.

## Step-by-Step Instructions

### Step 1: Identify Write Failures

When `write_file` fails repeatedly on the same file:
1. Note the error message (e.g., "unknown error", encoding issues)
2. Review the file content for special characters, nested quotes, or complex syntax
3. Attempt the heredoc workaround below

### Step 2: Use Heredoc Syntax

Execute shell command with heredoc:

```bash
cat > /path/to/file.tsx << 'EOF'
// Your file content here
import React from 'react';
const Component = () => {
  return <div>Hello</div>;
};
export default Component;
EOF
```

### Step 3: Key Heredoc Rules

1. **Use quoted delimiter** (`'EOF'` not `EOF`) to prevent variable expansion:
   - `'EOF'` = literal content (recommended for code files)
   - `EOF` = allows variable expansion (use only if needed)

2. **Match delimiters exactly**: Opening and closing `EOF` must be on their own lines with no leading/trailing whitespace

3. **No escaping needed**: Content between delimiters is taken literally when using quoted delimiter

### Step 4: Verify File Creation

After executing the heredoc command:
1. Check if file exists: `ls -la /path/to/file.tsx`
2. Verify content: `cat /path/to/file.tsx` or `head -20 /path/to/file.tsx`
3. Proceed with subsequent tasks once confirmed

## Example Scenarios

### TypeScript/JSX Component

```bash
cat > src/components/Button.tsx << 'EOF'
import React from 'react';

interface ButtonProps {
  label: string;
  onClick: () => void;
}

export const Button: React.FC<ButtonProps> = ({ label, onClick }) => {
  return (
    <button onClick={onClick} className="btn-primary">
      {label}
    </button>
  );
};
EOF
```

### JSON Configuration

```bash
cat > tsconfig.json << 'EOF'
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "commonjs",
    "strict": true,
    "esModuleInterop": true
  },
  "include": ["src/**/*"]
}
EOF
```

### Complex Shell Script

```bash
cat > deploy.sh << 'EOF'
#!/bin/bash
set -e

echo "Deploying..."
if [ "$ENV" = "production" ]; then
  echo "Production deployment"
fi
EOF
```

## Advantages Over write_file

1. **No escaping complexity**: Quotes, backslashes, and special characters work naturally
2. **Preserves formatting**: Indentation and line breaks remain exact
3. **Handles multi-line content**: No need to concatenate strings or use base64
4. **More reliable**: Shell handles file I/O directly with fewer abstraction layers

## Limitations

- Creates files in shell execution context (ensure correct working directory)
- Less programmatic than `write_file` for dynamic content generation
- Requires shell access (may not work in restricted environments)

## Troubleshooting

**Problem**: File not created at expected path
- **Solution**: Use absolute paths or verify current working directory with `pwd`

**Problem**: Content appears corrupted
- **Solution**: Ensure delimiter is quoted (`'EOF'`) and closing delimiter has no trailing whitespace

**Problem**: Permission denied
- **Solution**: Ensure target directory exists and you have write permissions