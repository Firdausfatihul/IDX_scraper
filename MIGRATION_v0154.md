# v0.15.4 True Incremental Boundary + Profile Delete

This release tightens normal incremental behavior and keeps historical repair explicit.

## Incremental boundary fixes

- A genuinely empty profile no longer receives an artificial successful watermark before its first completed run.
- Existing archives can bootstrap the poll boundary from the latest locally saved **completed** run report. This preserves the requested poll end separately from the timestamp of the last announcement seen.
- If the requested end is already covered by the saved successful poll watermark, normal incremental mode becomes a metadata no-op: no IDX metadata request, attachment download, extraction, or OpenRouter work is started.
- Calendar-day overlap may still be queried when moving forward, but announcements at or before the trusted successful poll boundary are discarded before cache lookup, database insert, attachment preparation, or LLM work. Normal mode therefore does not silently backfill trusted history.
- Partial or failed runs do not advance the successful poll watermark.
- Historical audit/backfill remains the explicit mode for repairing old metadata.

## Adaptive IDX wide-page recovery

The old fixed 200-row rescue was too small for observed busy days such as 321 disclosures. v0.15.4 keeps normal pages small, but when offset pagination is inconsistent it chooses a verified one-shot page large enough for the server-reported shard, bounded by `IDX_WIDE_PAGE_PROBE_MAX_SIZE` (default 1000). If the server returns fewer rows than it reports, the shard is still marked incomplete.

Defaults:

```env
IDX_WIDE_PAGE_PROBE_SIZE=200
IDX_WIDE_PAGE_PROBE_MAX_SIZE=1000
```

## Profile deletion

The profile toolbar now includes **Delete profile**.

- Main archive is protected and cannot be deleted.
- Deletion requires an explicit browser confirmation describing the permanent local data removal.
- A running/queued scraper blocks profile deletion.
- Deleting the active non-main profile switches the workspace to Main first, then removes that profile's isolated directory and registry entry.
- The deleted profile's SQLite database, raw attachments, extracted text, summaries, browser profile, traces, exports, and saved runs are removed with that isolated profile directory.

## Ticker field clarity

The ticker input now says `Blank = ALL companies` and explicitly notes that leaving it blank scrapes all listed companies.

## LLM generation

No OpenRouter model, output-token ceiling, schema, or generation capability is reduced in v0.15.4.
