# v0.13.0 · Intelligence Triage

This is the cumulative Phase 3 release. It may be installed directly over v0.10.0, v0.11.0, or v0.12.0. The upgrade archive does not contain `data/`, `.env`, SQLite databases, browser profiles, downloads, extracted text, or run history.

## Routine filing triage

`Laporan Bulanan Registrasi Pemegang Efek` is no longer automatically fanned out into one LLM call per attachment when the evidence is low-risk and bounded. The pipeline still downloads and extracts every selected source first. A conservative deterministic scan then routes the announcement:

- `routine_direct`: all retained raw evidence is sent in one strict structured announcement call;
- `full`: existing document summaries plus announcement reducer are retained.

Any detected control/ownership/director transaction signal, sparse evidence, selected-source extraction failure, or evidence beyond the configured size guard keeps the full pipeline. The routing hint is not treated as a source fact.

## Safe duplicate suppression

After extraction, exact duplicates and high-confidence near duplicates can be excluded from LLM analysis. Near-duplicate matching requires the same file suffix, high length overlap, and a very high text-similarity threshold. This intentionally prevents a financial XLSX from suppressing a PDF report or vice versa.

SQLite attachments retain the source row and now record `duplicate_of_url`. Announcement audit views show the exclusion reason.

## Adaptive provider concurrency

A new provider-level request gate sits under the global LLM job scheduler. The configured Global LLM slots remain the hard ceiling. On HTTP 429 the current request limit is halved; transient/5xx failures reduce it by one; sustained healthy responses ramp the limit back up. The run report records provider-gate metrics.

## Database migration

Existing databases are migrated in place with additive columns only:

- `attachments.duplicate_of_url`
- `announcement_summaries.analysis_mode`
- `announcement_summaries.triage_json`

No existing summaries or source files are deleted by the migration itself.

## New controls

GUI/profile autosave:

- Adaptive provider slots
- Routine filing triage
- Safe duplicate suppression

CLI:

```text
--adaptive-llm / --fixed-llm
--routine-triage / --no-routine-triage
--safe-dedup / --no-safe-dedup
```
