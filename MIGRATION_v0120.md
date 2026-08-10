# v0.12.0 · Pipeline Engine II

Phase 2 is cumulative with v0.11.0. It can be overlaid directly on a v0.10.0 project; installing v0.11.0 first is not required.

## What changed

- Adaptive attachment transport remembers successful fallbacks for the current run:
  - direct HTTP 403 → use Chromium for remaining attachments;
  - Chromium request-context challenge + successful page fetch → use in-page fetch directly for remaining attachments.
- Local extraction is separated from browser I/O and runs in a bounded ticker-fair worker pool.
- Extraction queue backpressure prevents unbounded PDF/OCR backlog.
- Global LLM scheduler fairness is enforced by ticker and disclosure group.
- A per-ticker active cap prevents one issuer with many disclosure groups from monopolizing all global slots.
- Weighted non-starving stage scheduling prioritizes document work, then announcement reducers, then company reducers.
- GUI/profile state adds `Extraction workers` and `Extraction backlog` controls.

## Intentionally unchanged

- No routine-disclosure fast path.
- No ownership anomaly heuristic.
- No aggressive attachment similarity deduplication.
- No concurrent use of the shared synchronous Playwright context from Python worker threads.
- Company/ticker isolation, prompt schemas, cache semantics, audit persistence, recovery, profiles, Library, Activity, and share exports remain intact.

## Suggested starting values

```text
Global LLM slots      4
Per disclosure cap    2
Extraction workers    3
Extraction backlog    8
```

The extraction backlog is the maximum active + queued local extraction work. Browser downloads pause when that bounded queue is full, then resume as workers free capacity.
