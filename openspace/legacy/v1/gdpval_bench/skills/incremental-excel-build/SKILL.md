---
name: incremental-excel-build
description: Build complex Excel files through staged, verifiable steps with intermediate CSV outputs for debugging
---

# Incremental Excel Build Pattern

When creating complex Excel files with calculations, forecasts, or data transformations, use an **incremental build-and-verify** approach instead of monolithic scripts. This pattern breaks the workflow into discrete, testable stages with intermediate CSV outputs that can be inspected at each step.

## When to Use

- Creating Excel files with multiple data sources
- Complex calculations or forecasts that need validation
- Tasks where debugging intermediate results is important
- Workflows that may need to be re-run from a specific stage

## The Four-Stage Pattern

### Stage 1: Data Extraction

Extract raw data from source systems and save to CSV.

```python
# extract_data.py
import pandas as pd

def extract_store_data():
    # Query database, API, or read source files
    stores = pd.read_csv('source_stores.csv')
    sales_history = pd.read_csv('source_sales.csv')
    
    # Save intermediate output for verification
    stores.to_csv('intermediate_stores.csv', index=False)
    sales_history.to_csv('intermediate_sales.csv', index=False)
    
    print(f"Extracted {len(stores)} stores, {len(sales_history)} sales records")
    return stores, sales_history

if __name__ == '__main__':
    extract_store_data()
```

**Verification checkpoint:** Open `intermediate_stores.csv` and `intermediate_sales.csv` to verify data completeness and format before proceeding.

### Stage 2: Data Preparation/Transformation

Clean, filter, and transform data for calculations.

```python
# prepare_data.py
import pandas as pd

def prepare_data():
    # Load intermediate files from Stage 1
    stores = pd.read_csv('intermediate_stores.csv')
    sales = pd.read_csv('intermediate_sales.csv')
    
    # Filter active stores, clean data
    active_stores = stores[stores['status'] == 'active']
    
    # Merge and prepare for calculations
    prepared = pd.merge(active_stores, sales, on='store_id', how='left')
    prepared = prepared.fillna(0)  # Handle missing values
    
    # Save for verification
    prepared.to_csv('intermediate_prepared.csv', index=False)
    
    print(f"Prepared data for {len(prepared)} store-week combinations")
    return prepared

if __name__ == '__main__':
    prepare_data()
```

**Verification checkpoint:** Review `intermediate_prepared.csv` to confirm filtering logic and data integrity.

### Stage 3: Calculations/Forecasts

Perform business logic, forecasts, or complex calculations.

```python
# calculate_forecast.py
import pandas as pd
import numpy as np

def calculate_forecast():
    # Load prepared data from Stage 2
    data = pd.read_csv('intermediate_prepared.csv')
    
    # Apply forecast logic
    data['forecast_week1'] = data['avg_sales'] * 1.05  # 5% growth
    data['forecast_week2'] = data['avg_sales'] * 1.08
    data['forecast_week3'] = data['avg_sales'] * 1.10
    data['forecast_week4'] = data['avg_sales'] * 1.12
    
    # Calculate totals and metrics
    data['total_forecast'] = data[['forecast_week1', 'forecast_week2', 
                                    'forecast_week3', 'forecast_week4']].sum(axis=1)
    
    # Save calculations for verification
    data.to_csv('intermediate_calculated.csv', index=False)
    
    print(f"Calculated forecasts with avg total: ${data['total_forecast'].mean():.2f}")
    return data

if __name__ == '__main__':
    calculate_forecast()
```

**Verification checkpoint:** Validate `intermediate_calculated.csv` for calculation accuracy and reasonableness of forecast values.

### Stage 4: Excel Output

Format and write final Excel file with proper styling.

```python
# create_excel.py
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

def create_excel_output():
    # Load calculated data from Stage 3
    data = pd.read_csv('intermediate_calculated.csv')
    
    # Create Excel writer
    writer = pd.ExcelWriter('final_output.xlsx', engine='openpyxl')
    data.to_excel(writer, sheet_name='Forecast', index=False)
    
    # Apply formatting
    workbook = writer.book
    worksheet = writer.sheets['Forecast']
    
    # Header styling
    header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF')
    
    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')
    
    # Format currency columns
    for col in ['forecast_week1', 'forecast_week2', 'forecast_week3', 
                'forecast_week4', 'total_forecast']:
        col_letter = list(data.columns).index(col) + 1
        for row in range(2, len(data) + 2):
            worksheet.cell(row=row, column=col_letter).number_format = '$#,##0.00'
    
    # Auto-adjust column widths
    for column in worksheet.columns:
        max_length = max(len(str(cell.value)) for cell in column)
        worksheet.column_dimensions[column[0].column_letter].width = min(max_length + 2, 20)
    
    writer.close()
    print("Created final_output.xlsx with formatting")

if __name__ == '__main__':
    create_excel_output()
```

**Verification checkpoint:** Open `final_output.xlsx` to verify formatting, data accuracy, and completeness.

## Runner Script

Create a main runner that orchestrates all stages:

```python
# run_pipeline.py
import subprocess
import sys

def run_stage(script_name, stage_name):
    print(f"\n=== Running {stage_name} ===")
    result = subprocess.run(['python', script_name], capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(f"ERROR in {stage_name}: {result.stderr}")
        sys.exit(1)
    return True

def main():
    stages = [
        ('extract_data.py', 'Data Extraction'),
        ('prepare_data.py', 'Data Preparation'),
        ('calculate_forecast.py', 'Forecast Calculation'),
        ('create_excel.py', 'Excel Output')
    ]
    
    for script, name in stages:
        run_stage(script, name)
    
    print("\n=== Pipeline Complete ===")

if __name__ == '__main__':
    main()
```

## Benefits

1. **Debugging**: If Stage 3 fails, you can inspect `intermediate_prepared.csv` without re-running extraction
2. **Verification**: Each stage produces inspectable output before proceeding
3. **Reusability**: Individual stages can be modified independently
4. **Transparency**: Stakeholders can review intermediate data
5. **Recovery**: Failed runs can resume from the last successful stage

## File Organization

```
project/
├── run_pipeline.py          # Main orchestrator
├── extract_data.py          # Stage 1
├── prepare_data.py          # Stage 2
├── calculate_forecast.py    # Stage 3
├── create_excel.py          # Stage 4
├── intermediate_stores.csv  # Stage 1 output (gitignore in production)
├── intermediate_sales.csv   # Stage 1 output
├── intermediate_prepared.csv # Stage 2 output
├── intermediate_calculated.csv # Stage 3 output
└── final_output.xlsx        # Stage 4 output (deliverable)
```

## Tips

- Add `.gitignore` entries for intermediate CSV files in production
- Include timestamp logging in each stage for audit trails
- Consider adding stage-specific unit tests
- For large datasets, add memory-efficient streaming in extraction stage