# v0.4.2 — Ticker audit trail and source provenance

v0.4.2 turns each saved company digest into a drill-down research record.

## GUI

- A ticker strip lets completed multi-company runs switch between every saved company.
- **Inspect ticker** opens five views: Summary, Process, Prompt → output, Claims & sources, and Announcements.
- Saved attachments and extracted text can be opened directly from the local GUI.
- Document `source_evidence` is located back to PDF page / extracted-text line when possible.
- Historical claim provenance is labeled exact, deterministic, inferred, or unresolved instead of pretending old data has perfect attribution.

## Exact LLM audit from v0.4.2 onward

Every OpenRouter attempt stores locally in SQLite:

- stage/schema/ticker/announcement/file identity;
- exact system prompt and exact rendered user prompt;
- raw response plus validated JSON;
- model, prompt profile/version and retry attempt;
- request start/end/elapsed time;
- prompt/completion/total token usage;
- success/failure and error text.

Historical runs did not persist raw prompts. The GUI therefore shows a clearly labeled reconstruction from saved checkpoint inputs where possible.

## Provenance for new company digests

The company schema now contains `claim_sources`, mapping material claims to source announcement IDs. Those IDs are filtered against announcements actually supplied to the reducer. The GUI deterministically expands them to the saved attachment filenames/URLs.

## Timing

New attachment checkpoints persist separate `downloaded_at` and `extracted_at` timestamps. Existing databases migrate in place; old rows remain readable and are not deleted.
