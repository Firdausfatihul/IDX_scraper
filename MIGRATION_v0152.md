# v0.15.2 Fast Completeness Recovery

This patch removes the common 962-ticker slow path for busy IDX calendar days when the reported result count can fit in one larger response.

## What changed

- Normal metadata requests keep the configured `IDX_PAGE_SIZE` (default 50).
- If offset pagination becomes inconsistent, a shard whose reported result count is no larger than `IDX_WIDE_PAGE_PROBE_SIZE` is retried once from `indexFrom=0` using that wider `pageSize` (default 200).
- A wide response is considered complete only when the number of collected rows reaches the reported result count. A server-side hard cap cannot masquerade as success merely because the response is shorter than the requested page size.
- Daily date sharding remains the first fallback for large multi-day windows.
- Stock-master per-ticker reconstruction remains available only as the final fallback when a daily shard cannot be proven complete with normal pagination or the wide-page probe.
- Activity events identify wide-page probe attempts, successes, skips, and failures.

## Why this replaces the proposed time split

`GetAnnouncement` is currently called with calendar-date `dateFrom` and `dateTo` parameters. The client has no verified intra-day time-range parameter to send to IDX, so pretending to split 00:00-11:59 versus 12:00-23:59 would not actually constrain the server query. v0.15.2 therefore uses a larger single-page recovery request, which is both implementable with the observed endpoint contract and dramatically cheaper for the real 183-row day that triggered the problem.

## Safety

- Incomplete metadata still remains incomplete. The scraper does not silently accept a truncated wide response.
- The v0.15.1 429 cooldown and paced ticker fallback remain intact.
- No database migration is required.
- No data directory, SQLite database, browser profile, or `.env` file is shipped in the upgrade archive.

## LLM generation limits

No OpenRouter output-token ceiling is reduced or modified in v0.15.2.
