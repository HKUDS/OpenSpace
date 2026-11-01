---
name: irregular-excel-parsing
description: Handle Excel files with irregular headers, merged cells, and unknown header row positions using pattern-matching and index-based extraction.
---

# Irregular Excel File Parsing

Use this skill when you encounter Excel files where standard `pandas.read_excel()` fails due to:
- Headers not in row 0 (6-9+ header rows common)
- Merged cells in header area
- Unknown or inconsistent header row positions
- Multiple title/metadata rows before actual data

## Step-by-Step Instructions

### Step 1: Read Excel Without Headers

First, read the entire sheet with `header=None` to get raw data:

```python
import pandas as pd

# Read all data without assuming header position
df_raw = pd.read_excel('filename.xlsx', sheet_name='Sheet1', header=None)
```

### Step 2: Scan Rows to Find Header Pattern

Search for the actual header row by looking for distinctive patterns:

```python
def find_header_row(df):
    """Find header row by pattern-matching common column identifiers."""
    header_patterns = [
        r'Store ID',
        r'ID\d{4}',  # ID followed by 4 digits
        r'Week \d+',
        r'Date',
        r'Store',
        r'Product',
        r'ID'
    ]
    
    for row_idx in range(len(df)):
        row_values = df.iloc[row_idx].astype(str).str.lower()
        for pattern in header_patterns:
            if row_values.str.contains(pattern, case=False, regex=True).any():
                return row_idx
    
    # Fallback: return first non-empty row
    for row_idx in range(len(df)):
        if df.iloc[row_idx].notna().sum() > 0:
            return row_idx
    
    return 0

header_row = find_header_row(df_raw)
```

### Step 3: Extract and Clean Headers

Extract the header row and clean column names:

```python
# Extract header row
headers = df_raw.iloc[header_row].tolist()

# Clean headers: convert to string, strip whitespace, handle NaN
clean_headers = []
for h in headers:
    if pd.isna(h) or str(h).strip() == '':
        clean_headers.append(f'col_{len(clean_headers)}')
    else:
        clean_headers.append(str(h).strip())

# Handle duplicate headers by adding suffix
from collections import Counter
header_counts = Counter(clean_headers)
final_headers = []
for h in clean_headers:
    if header_counts[h] > 1:
        final_headers.append(f"{h}_{header_counts[h]}")
        header_counts[h] -= 1
    else:
        final_headers.append(h)
```

### Step 4: Extract Data Rows

Extract data starting from the row after headers:

```python
# Get data rows (everything after header)
data_df = df_raw.iloc[header_row + 1:].copy()
data_df.columns = final_headers

# Reset index
data_df = data_df.reset_index(drop=True)

# Remove completely empty rows
data_df = data_df.dropna(how='all')
```

### Step 5: Validate and Clean Data

Perform basic validation and type conversion:

```python
# Identify ID columns and preserve as string
for col in data_df.columns:
    if 'id' in col.lower() or 'code' in col.lower():
        data_df[col] = data_df[col].astype(str).str.strip()

# Convert numeric columns
numeric_cols = data_df.select_dtypes(include=['float64', 'int64']).columns
for col in numeric_cols:
    data_df[col] = pd.to_numeric(data_df[col], errors='coerce')

# Remove rows with invalid critical data
if 'Store ID' in data_df.columns:
    data_df = data_df[data_df['Store ID'].notna() & (data_df['Store ID'] != '')]
```

## Complete Example Function

```python
def parse_irregular_excel(filepath, sheet_name=0):
    """Parse Excel file with unknown/irregular header structure."""
    import pandas as pd
    import re
    from collections import Counter
    
    # Step 1: Read raw
    df_raw = pd.read_excel(filepath, sheet_name=sheet_name, header=None)
    
    # Step 2: Find header row
    patterns = [r'Store ID', r'ID\d{4}', r'Week', r'Date', r'Store']
    header_row = 0
    for idx in range(min(15, len(df_raw))):  # Check first 15 rows
        row_str = ' '.join(df_raw.iloc[idx].astype(str))
        for pattern in patterns:
            if re.search(pattern, row_str, re.IGNORECASE):
                header_row = idx
                break
    
    # Step 3: Extract headers
    headers = [str(h).strip() if pd.notna(h) else f'col_{i}' 
               for i, h in enumerate(df_raw.iloc[header_row])]
    
    # Handle duplicates
    counts = Counter(headers)
    final_headers = []
    for h in headers:
        if counts[h] > 1:
            final_headers.append(f"{h}_{counts[h]}")
            counts[h] -= 1
        else:
            final_headers.append(h)
    
    # Step 4: Extract data
    data_df = df_raw.iloc[header_row + 1:].copy()
    data_df.columns = final_headers
    data_df = data_df.dropna(how='all').reset_index(drop=True)
    
    return data_df
```

## When to Use This Pattern

- ✅ Excel files with 6-9+ title/metadata rows before data
- ✅ Merged cells in header area causing misalignment
- ✅ Headers not in predictable positions
- ✅ Multiple sheets with inconsistent structures

## When NOT to Use

- ❌ Standard Excel files with headers in row 0 (use `read_excel()` directly)
- ❌ Files with consistent, known structure (use explicit `header=` parameter)
- ❌ When you know exact header row position (specify it directly)

## Tips

1. **Always inspect first**: Use `df_raw.head(20)` to visualize structure before parsing
2. **Pattern flexibility**: Adjust regex patterns based on your specific column naming conventions
3. **Handle merged cells**: Merged cells often result in NaN values - fill strategically if needed
4. **Save for reuse**: Once you determine the correct header row for a file type, cache this for future runs