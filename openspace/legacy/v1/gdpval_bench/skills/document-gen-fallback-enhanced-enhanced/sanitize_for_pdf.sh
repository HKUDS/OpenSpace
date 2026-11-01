#!/bin/bash
# sanitize_for_pdf.sh - Replace problematic unicode characters for LaTeX/PDF generation
# 
# Usage: ./sanitize_for_pdf.sh <input.md> [output.md]
# 
# If output file is not specified, creates <input>_sanitized.md
# 
# This script replaces Unicode characters that commonly cause issues with
# pdflatex and other LaTeX-based PDF engines.

set -e

if [ -z "$1" ]; then
  echo "Usage: $0 <input.md> [output.md]"
  echo "Example: $0 source.md source_sanitized.md"
  exit 1
fi

INPUT="$1"
OUTPUT="${2:-${1%.md}_sanitized.md}"

# Verify input file exists
if [ ! -f "$INPUT" ]; then
  echo "Error: Input file '$INPUT' not found"
  exit 1
fi

# Perform character replacements
# Order matters for some overlapping patterns
sed -e 's/—/--/g' \
    -e 's/–/-/g' \
    -e 's/"\([^"]*\)"/"\1"/g' \
    -e "s/'\([^']*\)'/\'\1\'/g" \
    -e 's/…/.../g' \
    -e 's/→/->/g' \
    -e 's/←/<-/g' \
    -e 's/↑/^/g' \
    -e 's/↓/v/g' \
    -e 's/•/-/g' \
    -e 's/✓/[x]/g' \
    -e 's/✗/[ ]/g' \
    -e 's/★/*/g' \
    -e 's/©/(c)/g' \
    -e 's/®/(r)/g' \
    -e 's/™/(tm)/g' \
    -e $'s/\t/    /g' \
    "$INPUT" > "$OUTPUT"

echo "✓ Sanitized: $INPUT -> $OUTPUT"
echo "  Characters replaced: em-dash, en-dash, curly quotes, ellipsis, arrows, bullets, symbols"
