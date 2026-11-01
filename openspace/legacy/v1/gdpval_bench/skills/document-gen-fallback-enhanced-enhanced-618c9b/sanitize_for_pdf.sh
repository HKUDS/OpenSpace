#!/bin/bash
# sanitize_for_pdf.sh - Replace problematic unicode chars for LaTeX/PDF conversion
# Usage: ./sanitize_for_pdf.sh <input.md> [output.md]

set -e

if [ -z "$1" ]; then
  echo "Usage: $0 <input.md> [output.md]"
  echo "  input.md  - Source markdown file with unicode characters"
  echo "  output.md - Sanitized output file (default: input_sanitized.md)"
  exit 1
fi

INPUT="$1"
OUTPUT="${2:-${1%.md}_sanitized.md}"

# Check input file exists
if [ ! -f "$INPUT" ]; then
  echo "Error: Input file '$INPUT' not found"
  exit 1
fi

# Perform character replacements
sed -e 's/—/--/g' \
    -e 's/–/-/g' \
    -e 's/"([^"]*)"/"\1"/g' \
    -e "s/'([^']*)/'\1'/g" \
    -e 's/…/.../g' \
    -e 's/→/->/g' \
    -e 's/←/<-/g' \
    -e 's/↑/^/g' \
    -e 's/↓/v/g' \
    -e 's/✓/[x]/g' \
    -e 's/✗/[ ]/g' \
    -e 's/★/*/g' \
    -e 's/●/-/g' \
    -e 's/©/(c)/g' \
    -e 's/®/(r)/g' \
    -e 's/™/(tm)/g' \
    "$INPUT" > "$OUTPUT"

# Verify output was created
if [ -f "$OUTPUT" ]; then
  echo "Sanitized: $INPUT -> $OUTPUT"
  echo "Input size:  $(wc -c < "$INPUT") bytes"
  echo "Output size: $(wc -c < "$OUTPUT") bytes"
else
  echo "Error: Failed to create output file"
  exit 1
fi
