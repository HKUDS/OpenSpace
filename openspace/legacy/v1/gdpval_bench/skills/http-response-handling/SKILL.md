---
name: http-response-handling
description: Handle websites requiring JavaScript by using curl with browser headers and validating file types.
---

# HTTP Response Handling for JavaScript-Dependent Sites

## When to Use This Skill

Use this technique when you need to fetch content from websites that:
- Render content dynamically with JavaScript
- Return placeholder HTML when accessed by non-browser clients
- Deliver different content based on User-Agent headers

## Core Technique

### Step 1: Fetch Content with Browser-Like Headers

Use `curl` with a realistic User-Agent header to mimic a real browser:

```bash
curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" -o output.html "https://example.com"
```

Key flags:
- `-L` — Follow redirects
- `-A` — Set User-Agent header to mimic a real browser
- `-o` — Save output to file for inspection

### Step 2: Detect Placeholder HTML Responses

After fetching, check if you received a placeholder response instead of actual content:

```bash
# Check file size (placeholder responses are often very small)
wc -c output.html

# Check for common placeholder indicators
grep -i "javascript" output.html | head -5
grep -i "loading" output.html | head -5
grep -i "noscript" output.html | head -5
```

Signs of a placeholder response:
- File size is suspiciously small (<5KB for content pages)
- Contains大量 JavaScript but minimal actual content
- Has "loading", "spinner", or "noscript" tags
- Missing expected text/data from the page

### Step 3: Validate File Type Before Parsing

Before attempting format-specific parsing, validate the file type:

```bash
# Check the file type
file output.html

# Check the actual content type (if you have the headers)
curl -I -A "Mozilla/5.0 ..." "https://example.com" | grep -i content-type

# Inspect first few lines
head -50 output.html
```

Common checks:
- **HTML files**: Should start with `<!DOCTYPE` or `<html`
- **JSON files**: Should start with `{` or `[`
- **PDF files**: Should start with `%PDF`
- **Empty/error pages**: May contain error messages or generic HTML

### Step 4: Handle Different Scenarios

**If you got valid HTML content:**
```bash
# Proceed with HTML parsing or extraction
grep -oP '(?<=<title>).*?(?=</title>)' output.html
```

**If you got a placeholder/JS-dependent response:**
- Option A: Use a headless browser (Playwright, Selenium)
- Option B: Look for an API endpoint that returns JSON directly
- Option C: Check if the site has a mobile/API version with simpler responses

**If you got an unexpected file type:**
```bash
# Check what was actually returned
file output.html

# Adjust your approach based on actual content
case $(file -b --mime-type output.html) in
  "text/html")
    # Parse as HTML
    ;;
  "application/json")
    # Parse as JSON
    ;;
  "application/pdf")
    # Handle as PDF
    ;;
  *)
    echo "Unexpected file type: $(file -b --mime-type output.html)"
    ;;
esac
```

## Quick Reference Script

```bash
#!/bin/bash
# fetch-with-validation.sh

URL="$1"
OUTPUT="${2:-output.html}"

# Fetch with browser headers
curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" -o "$OUTPUT" "$URL"

# Get file info
SIZE=$(wc -c < "$OUTPUT")
TYPE=$(file -b --mime-type "$OUTPUT")

echo "Downloaded: $OUTPUT"
echo "Size: $SIZE bytes"
echo "Type: $TYPE"

# Warn about potential issues
if [ "$SIZE" -lt 1000 ]; then
    echo "WARNING: File is very small - may be a placeholder response"
fi

if [ "$TYPE" = "text/html" ]; then
    if grep -qi "javascript\|loading\|spinner" "$OUTPUT"; then
        echo "WARNING: Content may be JavaScript-dependent"
    fi
fi
```

## Common Pitfalls

1. **Not checking file type**: Assuming HTML when you got JSON or an error page
2. **Using default curl User-Agent**: Many sites block or serve minimal content to bots
3. **Ignoring file size**: Very small files are often error pages or placeholders
4. **Not following redirects**: Some sites redirect based on User-Agent