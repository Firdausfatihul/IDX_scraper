# v0.11.0 · Pipeline Engine

Substantial Phase 1 orchestration upgrade. Existing data, profiles, prompts, raw files, extracted text, summaries, run history, and browser profiles remain in place.

## Phase 1

- Collect all available IDX metadata pages before announcement processing. A later metadata failure still leaves earlier collected metadata recoverable.
- Fair global LLM scheduler across tickers instead of concurrency only within the currently processed announcement.
- `LLM_CONCURRENCY` is the market-wide LLM worker budget.
- New `LLM_PER_ANNOUNCEMENT_CONCURRENCY` default `2` prevents one disclosure from taking every slot.
- Announcement reducers are queued only after their document jobs commit or fail.
- Company reducers run concurrently across tickers after announcement work drains.
- Document, announcement, and company summaries commit immediately using independent SQLite connections.
- Browser/Playwright work remains on its owning thread.
- Ticker isolation is asserted before reducers save results, and announcement identity fields are forced from the database row.
- Concurrent raw JSON streaming is suppressed to avoid interleaved output.
- Scheduler queue/dispatch/completion events and metrics are persisted in the run stream/report.
- Existing live combined share snapshots remain enabled, protected by a single-writer lock.

Example:

```env
LLM_CONCURRENCY=4
LLM_PER_ANNOUNCEMENT_CONCURRENCY=2
```

No routine-report fast path, anomaly heuristic, similarity deduplication, browser parallelism, or adaptive provider throttling is included yet. Those are intentionally deferred so Phase 1 improves throughput without changing analytical scope.
