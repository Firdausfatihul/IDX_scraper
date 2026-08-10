# v0.4.0: Cached company reducer

v0.4.0 adds a recovery-first company reduction path for interrupted all-market runs.

## Why

Older runs could leave hundreds of document summaries and announcement summaries in SQLite while only a few final company-window digests were produced. Re-running the scraper was wasteful because the expensive middle-stage work already existed.

## New command

```bash
idx-digest reduce-cached \
  --start 2026-07-06 \
  --end 2026-08-06 \
  --llm-concurrency 3
```

This mode:

- reads committed announcement summaries from SQLite;
- accepts legacy v0.3.7 announcement summaries as reducer input;
- skips company summaries that already exist for the exact window;
- ignores blank ticker rows;
- runs company reducers globally across tickers using the configured worker count;
- commits and exports each completed company immediately;
- never contacts IDX, launches Chromium, downloads attachments, extracts files, or reruns document/announcement summaries;
- can be rerun safely after a network interruption because already completed company digests are skipped.

Use `--force` only when you intentionally want to replace existing company summaries for the exact window with the current company prompt/model.

## GUI

The run panel now includes **Finish cached company digests**. Set the original date window and optional ticker, choose the worker count, and click the button. The same persistent run/event system used by normal runs tracks reducer progress and makes interrupted reducer runs resumable.

## Circuit breaker

When all active reducer lanes fail with network/OpenRouter-style errors before another success, v0.4.0 stops feeding new tickers into the worker pool. Completed company digests remain committed. Run the same cached reducer again after connectivity returns to continue with only the missing tickers.
