# v0.4.4 — Smart financial-source hotfix

This is a patch release for v0.4.3.

- Cached financial refinement now merges announcement raw JSON with the durable attachment table.
- Financial reports prefer XLSX + human-readable `LK` / `Laporan Keuangan` PDF.
- Generic `FinancialStatement*.pdf` is skipped when a better LK/report PDF exists, and used only as a fallback.
- `refine-financials --dry-run` previews keep/skip decisions and local cache availability with zero LLM calls.
- Refinement preflights the full selected source set and will not spend LLM calls on a partial financial bundle.
- Financial document chunks are reduced to at most 22,000 characters for more reliable structured output.
- Document JSON output budget is increased and malformed-JSON retries change strategy instead of resending the identical request.
- OpenRouter finish reason is retained in diagnostics/audit usage.
- No existing PDFs, extracted text, summaries, prompts, or database rows are deleted during upgrade.
