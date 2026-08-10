# v0.4.3 — Smart financial-statement sources

v0.4.3 prevents administrative financial-report attachments from diluting financial analysis.

## Default attachment policy

New runs use `smart` attachment selection.

For announcements whose title identifies a financial report, the analysis source set is restricted to:

- `FinancialStatement*.xlsx` / `.xlsm` workbooks;
- the actual financial-statement PDF, recognized by names such as `LK ...`, `Laporan Keuangan ...`, or `FinancialStatement ...`.

The pipeline records and skips before download/LLM:

- checklist/check-list documents;
- director/management statements and `Surat Pernyataan` files;
- `inlineXBRL.zip`, `instance.zip`, and other ZIP packages;
- unrelated supporting files in the same financial-report bundle.

Every attachment row now stores `selected_for_analysis`, `selection_reason`, and `selection_category`. Announcement reducers query only selected rows.

## PJAA / ABDA examples

PJAA keeps:

- `FinancialStatement-2026-II-PJAA.xlsx`
- `LK PJAA 30 Juni 2026.pdf`

ABDA keeps:

- `FinancialStatement-2026-II-ABDA.xlsx`
- `Laporan Keuangan TW II 2026 - ABDA.pdf`

## XLSX extraction

Formatted blank cells no longer consume the spreadsheet extraction budget. Rows are trimmed after the last populated value and emitted with `ROW <n>` markers under each `===== SHEET: ... =====` heading.

## Cached financial refinement

Use existing local data without contacting IDX or redownloading files:

```bash
idx-digest refine-financials --start 2026-07-06 --end 2026-08-06 --ticker PJAA
```

Omit `--ticker` to refine all cached financial-report announcements in the window. Selected financial documents are re-summarized with the current prompt; XLSX files are re-extracted with the improved reader first. Affected announcement and company reducers are rebuilt and committed immediately.

The GUI exposes the same action as **Refine cached financial reports**.

## Stock-only default

New scrapes default to the IDX stock scope (`emitenType=s`). The GUI switch **Listed stocks only** is enabled by default. Disable it, or use `--instrument-scope all`, only when intentionally collecting other IDX instruments.
