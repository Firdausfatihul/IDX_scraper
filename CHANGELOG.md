# Changelog

All notable changes and migration requirements are consolidated here. Upgrades preserve `.env`, SQLite data, downloaded attachments, extracted text, prompt profiles, run history, and the browser profile unless an entry explicitly says otherwise.

Use [INSTALLATION.md](INSTALLATION.md) for the current upgrade procedure. Historical archive/patch extraction commands have been removed because they no longer describe the current source tree.

## Unreleased

### Date picking for the share export

- Added a date selector to the **Copy / export all** panel: *Current scope* (previous behaviour), *Pick dates* (calendar pickers), and *All saved dates*.
- Listed every saved digest window with its company count, always visible and individually clickable. Picking dates preselects the windows they touch; clicking one includes or excludes it. Because saved windows overlap, a calendar range alone cannot always isolate one window — clicking can.
- Reported the current selection live and disabled the export buttons when nothing is selected, so an empty pick fails before the user commits rather than as a 404.
- Added `GET /api/share/windows`; `ShareRequest` gained `date_mode` (`exact`/`range`/`all`), `per_ticker` (`latest`/`all`), and `window_keys` for naming saved windows outright. Unknown window keys are ignored rather than widening the export.
- Added `export-all --range`, `--all-dates`, and `--every-window` for the same selection from the CLI.
- Range and all-dates exports advertise the span the selected digests actually **cover**, note the dates that were picked when those differ, and label every digest with its own window — a picked range routinely pulls in a window reaching outside it, and the artifact now says so. Export filenames carry the covered span.
- When one company has several selected windows, its newest **covered period** wins, not its most recently regenerated row.

Existing exact-window exports are byte-identical to before. No database migration.

## 0.15.5

### Coverage-aware incremental runs

- Replaced end-only no-op logic with normalized, scope-specific metadata coverage ranges.
- Subtracts known coverage from the exact requested interval and queries every missing gap.
- Supports backward, forward, and disjoint gaps.
- Stops new coverage at the poll-start snapshot.
- Imports only completed historical reports that prove both boundaries.
- Rejects false no-op, partial, filtered, capped, reducer, and refiner reports as coverage evidence.
- Keeps the legacy watermark for compatibility without treating it as proof of an unknown start.
- Leaves stock-master fanout disabled for normal incremental gaps; historical audit retains it.

Database migration is additive. Existing archives and summaries remain intact. Model, prompt, schema, and output ceilings are unchanged.

## 0.15.4

### Incremental boundary and profile deletion

- Distinguished the last successful poll boundary from the latest announcement timestamp.
- Added adaptive wide-page recovery up to a configured maximum.
- Added permanent deletion for non-Main isolated profiles, blocked during active work.
- Clarified that a blank ticker means all companies.

The Main profile cannot be deleted. This release's end-only no-op behavior was superseded by 0.15.5 coverage ranges.

## 0.15.3

### Incremental watermark mode

- Made forward-moving incremental retrieval the normal mode.
- Added a configurable overlap for recent disclosures.
- Reserved expensive per-ticker completeness reconstruction for historical audit.
- Prevented failed or partial runs from advancing trusted progress.

The watermark remains as compatibility and diagnostic state after 0.15.5.

## 0.15.2

### Fast completeness recovery

- Added a verified wide-page probe for inconsistent IDX offset pagination.
- Avoided unreliable intra-day splitting because the endpoint accepts calendar-day request bounds.
- Retained paced per-ticker reconstruction as historical last-resort recovery.

## 0.15.1

### IDX throttling hotfix

- Separated HTTP 429 handling from browser-verification handling.
- Honored `Retry-After` where supplied and added bounded cooldown with jitter.
- Paced historical per-ticker recovery with burst rests.
- Updated installation verification to check required prompt names rather than a fixed count.

## 0.15.0

### Incremental intelligence and recovery

- Added pagination completeness guards and partial-run reporting.
- Added fingerprinted company-window caches and dirty-company scope.
- Added deterministic single-announcement promotion.
- Streamed company checkpoints progressively to the GUI.
- Added phase-aware ETA and network recovery watchdog behavior.
- Added concurrency presets without changing generation ceilings.
- Distinguished active-run settings from next-run settings.
- Added Public Expose specialist analysis, financial-sheet ranking, routine ownership audit detail, and stock-master guardrails.

Database migrations are additive. No data reset is required.

## 0.14.4

### Financial fairness hotfix

- Recognized standalone `LK` statement PDFs as primary evidence.
- Added priority yield so queued foreground reducers are not starved by new bulk chunks.
- Separated generating work from provider/disclosure/validation waits in live counters.
- Added request-class latency estimates.

## 0.14.3

### Long Document Engine

- Added exact chunk plans, bounded intra-document parallelism, chunk checkpoints, retry reasons, and a conservative ordered combine stage.
- Improved tail ETA for outstanding chunks and combine work.

## 0.14.2

### Live request progress

- Added correlated request lifecycle states and live OpenRouter work cards.
- Added estimated latency progress for non-streaming requests and exact pipeline counters.
- Added received-character/chunk telemetry for streaming mode.

## 0.14.1

### Responsive GUI containment

- Prevented long IDs, URLs, JSON, and paths from expanding the page beyond the viewport.
- Added local ledger scrolling and narrower responsive layouts.

## 0.14.0

### Pipeline Observatory

- Added live scheduler, extraction, provider, throughput, latency, and ETA metrics.
- Persisted a performance report and conservative next-run tuning advice.
- Kept source selection and mid-run concurrency policy unchanged.

## 0.13.0

### Intelligence triage

- Added deterministic routine-filing triage after extraction.
- Added safe post-extraction duplicate suppression with audit records.
- Added adaptive provider concurrency and GUI controls for each optimization.

Source download/extraction remains mandatory before these decisions. XLSX and PDF evidence are not near-deduplicated against each other.

## 0.12.0

### Pipeline Engine II

- Added learned browser attachment transport, bounded background extraction, ticker-fair LLM scheduling, weighted stage priority, and extraction controls.
- Retained single-owner browser access and bounded backpressure.

## 0.11.0

### Pipeline Engine

- Replaced serial announcement execution with a fair market-wide scheduler.
- Added global and per-disclosure logical limits while keeping SQLite checkpoints immediate.

## 0.10.0

### Signal Desk Library

- Rebuilt the GUI as a durable Desk, Library, Companies, and Activity workspace.
- Added isolated research profiles and saved UI state.
- Preserved the original `data/` tree as the Main archive.

## 0.4.4

### Smart financial-source hotfix

- Merged cached announcement JSON with durable attachment rows during financial refinement.
- Preferred XLSX plus human-readable `LK`/report PDF and used generic statement PDFs as fallback.
- Added zero-cost dry-run selection previews and full-bundle preflight.
- Reduced financial chunks to at most 22,000 characters and improved malformed-JSON retry headroom.

## 0.4.3

### Smart financial-statement sources

- Added primary financial source selection and persisted selection reasons.
- Trimmed formatted workbook tails and labeled extracted rows.
- Added cached financial refinement and listed-stocks-only default scope.

## 0.4.2

### Ticker audit and provenance

- Added ticker inspection for process, prompt/output, claims/sources, and attachments.
- Persisted exact LLM request/response audits for new work.
- Added company claim-source IDs and separate download/extraction timestamps.

Historical prompts that were never stored remain explicitly labeled reconstructed.

## 0.4.1

### Shareable all-company exports

- Added zero-LLM combined Markdown/TXT exports in the GUI and CLI.
- Kept issuer boundaries explicit and refreshed combined files atomically after commits.

## 0.4.0

### Cached company reducer

- Added `reduce-cached` for completing company windows without contacting IDX or repeating earlier stages.
- Added immediate commits, resumability, legacy announcement support, and a network-failure circuit breaker.

Use `--force` only when intentionally replacing an existing exact-window company summary.

## 0.3.9

### Durable overnight runs and recovery

- Persisted active GUI runs, event history, partial checkpoints, and recovery exports.
- Marked unfinished runs interrupted after restart and added resume/open-checkpoint workflows.

## 0.3.8

### Prompt Studio and expanded schemas

- Added editable prompt layers, template validation, profile hashes, and selective cache invalidation.
- Added structured corporate-action, expansion, management/control, capital, regulatory, and scenario fields.
- Kept few-shot examples as classification guidance only; issuer facts must come from issuer evidence.

## 0.3.7

### Local Signal Desk GUI

- Added the local FastAPI workspace, background runs, SSE progress, and overlap prevention while retaining the CLI.

## 0.3.6

### Calm diagnostics

- Separated concise terminal progress from detailed JSONL and Playwright traces.
- Made browser network, cache, page, and raw-stream output explicitly opt-in.

## 0.3.5.post1

### Complete-source repair

- Restored package modules omitted from an earlier partial 0.3.5 archive.
- Preserved all runtime data and caches.

## 0.3.5

### Parallel document summaries

- Parallelized independent document-summary calls within one announcement.
- Kept shared-browser access and SQLite writes controlled.
- Disabled simultaneous raw token streams to prevent interleaved JSON.

## 0.3.4

### Diagnostics and tracing

- Added timestamped diagnostics, progress, browser tracing, structured streaming, and slowdown reporting.

## 0.3.3

### Summary-schema repair

- Added strict structured output validation and automatic regeneration of invalid cached objects such as `{}`.
- Required no database deletion; valid files and summaries remained reusable.
