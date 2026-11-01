---
name: excel-subtotal-audit
description: Audit generated Excel reports by enumerating populated output rows, exposing formulas, and checking that each subtotal range includes all visible detail rows.
---

# Excel Subtotal Audit

Use this workflow after generating or modifying an Excel report to catch silent spreadsheet defects that simple file-exists or sheet-exists checks will miss.

The core idea is:

1. Read back the finished workbook.
2. Print every populated output row, including formulas.
3. Identify section subtotal rows.
4. Independently verify that each subtotal formula includes all visible detail rows intended for that section.
5. Treat any mismatch as a report defect, even if the workbook opens successfully and looks formatted correctly.

This is especially useful for generated financial statements, operational summaries, rollups, grouped exports, and any worksheet where formulas summarize nearby detail rows.

## When to use

Use this skill when:

- You generate Excel workbooks programmatically.
- The workbook contains subtotal, total, or rollup formulas.
- Sections may contain blank rows, hidden rows, filtered rows, or varying detail lengths.
- A mistake in a formula range would produce a plausible but wrong spreadsheet.
- Basic validation only checks whether the file was created, whether sheets exist, or whether formulas are present.

## Goal

Do not stop at "the report was written successfully."

Instead, prove that:

- every expected output row is present,
- formulas were written into the intended cells,
- each subtotal references the correct detail range,
- no visible detail rows are omitted from the subtotal span.

## Workflow

## 1. Save the workbook, then reopen it for audit

Always audit the workbook after writing it, using a fresh read from disk if possible. This verifies the actual persisted artifact rather than in-memory assumptions.

Checklist:

- Save workbook.
- Reopen workbook from disk.
- Select the target worksheet.
- Use formula view or raw formulas, not only calculated values.

Why: a writer may have inserted the wrong range, overwritten cells, or shifted rows during formatting.

## 2. Enumerate every populated row in the output region

Print a row-by-row audit log of all populated rows in the report area.

For each row, print:

- row number,
- displayed labels or key identifiers,
- numeric cells,
- raw formulas for formula cells,
- whether the row is hidden, if your library exposes that,
- any grouping/outline level if relevant.

This creates a human- and machine-inspectable trace of what the generator actually produced.

### Minimum row audit format

A useful printed line looks like:

- Row 12: ["Revenue", 1250, "=SUM(B8:B11)"]
- Row 13: hidden ["Adjustment", 25, ""]
- Row 14: ["Total Revenue", "", "=SUM(B8:B13)"]

The exact format does not matter as much as consistency and inclusion of formulas.

### What counts as populated

A row is populated if at least one relevant output cell contains:

- a value,
- a string label,
- a formula,
- formatting that corresponds to a semantic output row you need to validate.

Prefer auditing a defined report column range rather than the entire sheet.

## 3. Locate subtotal rows explicitly

Identify rows that represent subtotals, totals, or section summaries.

Common signals:

- a label like "Subtotal", "Total", "Net", "Gross", "Section Total",
- a formula in a numeric column,
- bold or styled summary rows,
- known report structure from the generator.

For each subtotal row, capture:

- section name or identifier,
- subtotal row number,
- subtotal formula text,
- the expected detail row span according to neighboring visible rows and section boundaries.

Do not assume the formula is correct just because a subtotal row exists.

## 4. Independently derive expected detail coverage

This is the key step.

For each section, determine which visible detail rows should contribute to the subtotal without using the subtotal formula itself as the source of truth.

Possible ways to derive the expected rows:

- rows between the section header and the subtotal row,
- rows until the next section header,
- rows matching a known indentation or outline level,
- rows with detail labels but excluding headers and subtotal labels,
- rows included by the source data mapping used to build the section.

The important rule:

The expected detail set must be computed independently from the formula being checked.

Otherwise, the audit only repeats the original mistake.

### Include only visible detail rows when visibility matters

If the report hides rows, collapses groups, or applies filters, inspect whether the subtotal is supposed to summarize:

- all underlying rows, or
- only currently visible detail rows.

This skill specifically targets the case where you want to ensure the subtotal includes all visible detail rows in the section.

If your report semantics differ, state them explicitly and audit against those rules.

## 5. Compare expected detail rows to formula references

Parse each subtotal formula and compare its referenced range against the independently derived detail rows.

Look for defects such as:

- formula starts too late,
- formula ends too early,
- skipped first or last detail row,
- blank spacer row included instead of a detail row,
- hidden visible-boundary mistakes,
- formula copied from another section without updating range,
- subtotal includes prior section rows,
- subtotal excludes detail rows added later.

### Typical failure pattern

A generated subtotal like:

=SUM(B8:B11)

may look valid, but if the section's visible detail rows are actually 8 through 12, row 12 has been silently omitted.

This is exactly the kind of defect this audit should catch.

## 6. Fail loudly on any mismatch

If the independently derived detail rows and formula-covered rows do not match, treat it as a generation failure.

Report:

- sheet name,
- subtotal row number,
- subtotal label,
- formula text,
- expected contributing rows,
- actual referenced rows,
- specific missing or extra rows.

Example failure message:

Subtotal audit failed on sheet "P&L", row 24 ("Total Operating Expenses"): formula =SUM(C18:C22) but visible detail rows are 18-23; missing row 23.

Do not soften these findings into warnings if correctness matters.

## 7. Keep the audit output in the execution log

Preserve the printed populated-row trace and subtotal comparison results in logs or artifacts.

This helps with:

- debugging row-shift defects,
- reviewing generator behavior,
- comparing versions of a report writer,
- proving report correctness in automated runs.

## Practical procedure

## A. Build a row inventory

For the target columns, collect for each row:

- row index,
- cell values,
- cell formulas,
- hidden status,
- semantic classification:
  - section header,
  - detail,
  - subtotal,
  - blank/spacer,
  - other.

If classification is ambiguous, use explicit report rules rather than guessing.

## B. For each subtotal row

1. Find the section it belongs to.
2. Determine visible detail rows in that section.
3. Extract rows referenced by the subtotal formula in the subtotal column.
4. Compare expected vs actual rows.
5. Emit PASS or FAIL.

## C. Review all populated rows manually if needed

Even with automated checks, print the full row trace so a reviewer can spot:

- duplicated rows,
- unexpected blank rows,
- data in wrong section,
- subtotal formula in wrong column,
- labels not aligned with formulas.

## Implementation guidance

You can implement this in any language with an Excel reader. The audit logic matters more than the library.

Useful capabilities:

- read cell values,
- read raw formulas,
- inspect hidden rows,
- iterate row-by-row,
- parse formula references, at least for simple SUM ranges.

If formulas are complex, start by auditing common subtotal patterns first, such as:

- =SUM(B8:B12)
- =SUBTOTAL(9,B8:B12)

Then extend as needed.

## Example audit logic

Pseudocode:

1. Open workbook.
2. For each row in report range:
   - collect values and formulas,
   - print row audit line,
   - classify row.
3. For each subtotal row:
   - derive expected visible detail rows from section structure,
   - parse formula references,
   - compare expected rows with referenced rows,
   - fail if mismatch.

## Example pseudocode

function audit_sheet(sheet):
  rows = collect_rows(sheet)
  print_populated_rows(rows)

  subtotals = [r for r in rows if r.type == "subtotal"]

  for subtotal in subtotals:
      expected_rows = derive_visible_detail_rows(rows, subtotal.section)
      actual_rows = rows_referenced_by_formula(subtotal.formula, subtotal.value_column)

      if expected_rows != actual_rows:
          raise Error(
              "Subtotal mismatch at row "
              + subtotal.row_number
              + ": expected "
              + repr(expected_rows)
              + " but formula covers "
              + repr(actual_rows)
          )

## Example Python sketch

This example is intentionally generic and should be adapted to your workbook structure.

from openpyxl import load_workbook
import re

def is_populated(cells):
    for cell in cells:
        if cell.value not in (None, ""):
            return True
    return False

def formula_text(cell):
    return cell.value if isinstance(cell.value, str) and cell.value.startswith("=") else None

def parse_simple_sum_rows(formula, target_col_letter):
    if not formula:
        return []
    m = re.fullmatch(rf"=SUM\({target_col_letter}(\d+):{target_col_letter}(\d+)\)", formula.replace("$", ""))
    if not m:
        return None
    start, end = map(int, m.groups())
    return list(range(start, end + 1))

def audit_sheet(path, sheet_name, start_row, end_row, cols, label_col_idx, value_col_letter):
    wb = load_workbook(path, data_only=False)
    ws = wb[sheet_name]

    inventory = []
    for r in range(start_row, end_row + 1):
        cells = [ws[f"{col}{r}"] for col in cols]
        if not is_populated(cells):
            continue

        hidden = ws.row_dimensions[r].hidden is True
        values = [c.value for c in cells]
        formulas = [formula_text(c) for c in cells]
        print(f"Row {r} hidden={hidden} values={values} formulas={formulas}")

        label = ws[f"{cols[label_col_idx]}{r}"].value
        value_formula = formula_text(ws[f"{value_col_letter}{r}"])

        row_type = "detail"
        label_text = str(label).strip().lower() if label is not None else ""

        if "total" in label_text or "subtotal" in label_text:
            row_type = "subtotal"

        inventory.append({
            "row": r,
            "hidden": hidden,
            "label": label,
            "row_type": row_type,
            "value_formula": value_formula,
        })

    current_section = []
    for i, row in enumerate(inventory):
        if row["row_type"] != "subtotal":
            current_section.append(row)
            continue

        expected_rows = [x["row"] for x in current_section if x["row_type"] == "detail" and not x["hidden"]]
        actual_rows = parse_simple_sum_rows(row["value_formula"], value_col_letter)

        if actual_rows is None:
            raise ValueError(f"Unsupported formula at row {row['row']}: {row['value_formula']}")

        if expected_rows != actual_rows:
            raise AssertionError(
                f"Subtotal mismatch at row {row['row']} label={row['label']!r}: "
                f"expected visible detail rows {expected_rows}, formula covers {actual_rows}"
            )

        current_section = []

This sketch is deliberately simple. In real use, strengthen row classification and section detection.

## Design rules

## Never trust generated subtotal formulas without independent verification

A formula can be syntactically valid and still semantically wrong.

## Print formulas, not just calculated values

A subtotal value might look plausible even when the range is wrong.

## Verify row coverage, not just subtotal existence

Checking "there is a total row" is insufficient.

## Use independent section logic

The audit should derive expected detail membership from report structure or source mapping, not from the formula under test.

## Prefer deterministic checks over visual inspection

Manual workbook opening can miss subtle omissions.

## Keep the audit close to generation

Run it immediately after writing the workbook so defects are caught before delivery.

## Common pitfalls

- Auditing only workbook creation success.
- Checking formula presence but not range correctness.
- Comparing displayed totals without checking omitted rows.
- Ignoring hidden rows when report semantics depend on visibility.
- Using the generator's intended row list rather than the actual written rows.
- Parsing only labels and never inspecting formulas.
- Assuming contiguous sections when blank rows or headers interrupt them.

## Minimum acceptance criteria

A generated report passes this skill only if:

- all populated output rows are enumerated in the audit log,
- subtotal rows are explicitly identified,
- each subtotal formula is inspected,
- expected contributing detail rows are derived independently,
- every subtotal range is confirmed to include all intended visible detail rows,
- mismatches fail the run.

## Adaptation notes

Adjust the workflow for:

- multiple subtotal columns,
- nested section totals,
- filtered reports using SUBTOTAL,
- non-contiguous formula references,
- worksheets with merged labels or outline groups.

Even when full formula parsing is hard, the row-by-row printed audit remains valuable and should still be kept.

## Definition of done

The audit is complete when you can answer, for every subtotal row:

- Which visible detail rows belong to this section?
- Which rows does the written formula actually reference?
- Do those sets match exactly?

If not, the report is not verified.