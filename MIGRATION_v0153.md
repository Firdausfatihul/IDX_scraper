# v0.15.3 Incremental Watermark Mode

This patch changes the default metadata strategy from historical completeness re-auditing to forward incremental ingestion.

## Default daily behavior

- The active profile keeps a persistent SQLite watermark per instrument/ticker scope.
- On the first v0.15.3 run with an existing archive, the newest stored announcement timestamp is trusted as the baseline.
- The effective IDX request starts from the baseline/watermark minus `IDX_INCREMENTAL_OVERLAP_DAYS` (default 1 day), bounded by the requested start date.
- Already-complete announcement IDs in the overlap are skipped before download/extraction/LLM scheduling.
- Existing announcements without a valid current announcement summary are not skipped; they remain recoverable and are retried.
- A fully successful run advances the poll watermark to the requested end boundary. Partial/failed runs do not advance it.

## Historical audit / backfill

The old expensive completeness reconstruction is still available, but it is now explicit:

- GUI: enable **Historical audit / backfill**.
- CLI: pass `--historical-audit`.

Historical audit uses the full requested range and may use date shards, wide-page recovery, and the paced stock-master per-ticker fallback when required.

Normal incremental mode never fans an incomplete day out across all listed tickers. If normal pagination and the wide-page probe cannot prove a recent shard complete, the run is marked partial and the watermark remains unchanged.

## Database migration

A new additive table is created automatically:

`scrape_watermarks(scope_key, last_successful_poll_end, last_seen_announcement_at, baseline_source, updated_at)`

No existing announcements, attachments, summaries, profiles, or company fingerprints are rewritten.

## LLM limits

No OpenRouter output-token ceiling, model generation ability, or existing LLM max-token setting is reduced in v0.15.3.
