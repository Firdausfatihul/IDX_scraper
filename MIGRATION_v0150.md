# v0.15.0 Incremental Intelligence & Recovery

This is a cumulative upgrade and can be overlaid directly on v0.10.0 or any later release.

## Data safety

The upgrade archive does not ship `data/`, `.env`, SQLite databases, browser profiles, or runtime caches. Existing profiles and checkpoints remain in place.

SQLite migration is additive. `company_window_summaries` gains:

- `input_fingerprint`
- `generation_mode`
- `source_announcement_count`

Existing company summaries without a fingerprint are treated as legacy and may be refreshed once so future exact reruns can be true cache hits.

## Important behavior changes

- IDX metadata pagination is completeness-checked. Unexpected empty pages trigger date-shard fallback instead of a false successful completion.
- Company reducers use content fingerprints and dirty-issuer scope. Unchanged company summaries are reused without OpenRouter.
- Single-announcement company windows can be promoted deterministically into company schema without a second LLM call. `reduce-cached --force` bypasses this promotion and explicitly regenerates the company digest with the LLM.
- Public Expose / Investor Presentation documents remain full LLM analysis and use their own prompt/cache lineage.
- Network transport failures pause new provider retries until connectivity probes succeed.
- Financial XLSX extraction prioritizes primary financial sheets and preserves formulas without cached values.

## LLM output limits

v0.15.0 does **not** lower LLM output-token limits. Existing generation ceilings remain unchanged. The release saves time and cost by removing redundant requests, not by constraining answer length.
