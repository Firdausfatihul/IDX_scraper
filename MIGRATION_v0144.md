# Migration to v0.14.4 · Fair Financial Hotfix

This cumulative patch can be overlaid on v0.10.0+ and does not ship runtime `data/`, SQLite databases, `.env`, or browser profiles.

## Fixes

- Recognizes standalone `LKTT` financial-report filenames as primary human-readable financial statements.
- Generic `FinancialStatement*.pdf` remains a fallback and is excluded when an LK/LKTT/Laporan Keuangan report is present.
- Provider gate gives newly freed slots to waiting foreground requests before queued bulk long-document chunks.
- Live Work Ledger separates generating, provider-waiting, disclosure-waiting, queued, and validating request states.
- Long-document cards separate completed/generating/waiting chunk counts.
- Request ETA history is bucketed by request class/size, including large financial chunks.

No database migration is required. Existing chunk checkpoints, summaries, profiles, and audit history remain valid.
