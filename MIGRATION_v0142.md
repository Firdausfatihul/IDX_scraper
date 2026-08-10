# v0.14.2 · Live Request Progress

v0.14.2 is a cumulative observability/UX patch on top of v0.14.1. It can be installed directly over v0.10.0 or any later release. No intermediate Phase 1-4 install is required.

## What changed

- Every OpenRouter request now carries a short unique `request_id` through the run event stream.
- Adds explicit lifecycle events for queued, provider-slot wait, sending, generation, response receipt, structured-output validation, completion, and failure.
- The GUI Live Work Ledger renders active OpenRouter requests even when no generic Rich progress task exists.
- Active cards show elapsed wall time, ticker/file/stage, attempt, prompt size, output cap, provider/model, and current lifecycle state.
- Non-streaming generation gets a clearly labelled **estimated** progress bar based on recent same-schema latency, falling back to the provider average. It caps below completion until a real response arrives.
- The ledger shows exact announcement completion plus active/queued LLM counts.
- Streaming mode emits chunk/character telemetry for request cards when streaming is actually enabled.
- Preserves the v0.14.1 responsive containment rules.

## Safety / provider behavior

This release does not raise request concurrency and does not bypass throttling. The Phase 3 adaptive provider gate still reduces concurrency on HTTP 429 and transient failures and only ramps back up after healthy responses.

No database migration is required.
