# Migration to v0.14.3 · Long Document Engine

This cumulative upgrade can be overlaid directly on v0.10.0+ and does not ship runtime `data/`, SQLite databases, `.env`, or browser profiles.

## Additions

- `LLM_DOCUMENT_CHUNK_CONCURRENCY` (default `2`, allowed `1..4`).
- `document_chunk_summaries` SQLite table for durable per-chunk checkpoints.
- Exact `llm-chunk` progress events and combine-stage events.
- Per-disclosure request gate at the actual OpenRouter call boundary.
- Chunk-aware live ETA and long-document ledger cards.

The migration is additive. Existing document/announcement/company summaries remain valid and no historical data is deleted.
