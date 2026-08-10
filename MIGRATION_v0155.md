# v0.15.5 Coverage-Aware Incremental Runs

This release replaces end-only incremental decisions with persistent coverage intervals.

## Requested range minus saved coverage

Each profile stores normalized coverage under the existing instrument/ticker scope key:

```text
requested interval
      minus
saved coverage ranges
      equals
metadata gaps to query
```

For coverage `Aug 7 00:00 → Aug 9 23:59`:

- request Aug 7–9: metadata no-op;
- request Aug 6–9: query the backward gap only;
- request Aug 7–10: query the forward gap only;
- request Aug 6–10: query both edge gaps;
- request Aug 8–9: metadata no-op.

Multiple blocks are retained and normalized. If Aug 1–3 and Aug 5–9 are covered, an Aug 1–9 request queries only the uncovered interval around Aug 4. IDX accepts calendar dates rather than exact timestamps, so network queries may overlap a covered boundary; covered rows are rejected before cache lookup, database insertion, attachment work, or LLM work.

Coverage is committed atomically only after all planned gaps complete without pipeline errors. Partial and failed runs leave prior coverage unchanged. Keyword-filtered and `max_announcements`-capped runs never establish coverage. A successful historical audit merges the pollable portion of its requested range because it explicitly rechecks that interval for completeness.

The persisted end is capped at the run's poll-start snapshot. A request extending later than that snapshot keeps a deferred suffix, allowing a later rerun to collect disclosures published after the first poll. Archived reports with a run timestamp are capped the same way during migration.

## Additive database migration

The new table is created automatically in every profile database:

```text
scrape_coverage_ranges(
  scope_key,
  covered_start,
  covered_end,
  baseline_source,
  updated_at
)
```

The legacy `scrape_watermarks` table is preserved for compatibility and last-seen diagnostics. Its `last_successful_poll_end` value has no lower boundary, so v0.15.5 never treats that row alone as proof that earlier history is covered.

Completed, non-no-op metadata reports are imported when they prove actual queried boundaries. v0.15.4 no-op reports are ignored because an earlier requested start may be exactly the unpolled interval that exposed this bug. Reducer/refiner reports and partial runs are also ignored. When no trustworthy start can be recovered, the requested interval is conservatively checked once and then stored as coverage.

Reports that predate explicit instrument-scope diagnostics are treated as stock-scope evidence only. They are never imported into an `all`-instrument coverage scope, where they could incorrectly hide non-stock disclosures.

## Operational behavior

- Normal incremental gap queries continue to disable stock-master ticker fanout.
- Historical audit still forces the complete requested range and may use the expensive completeness fallback.
- Existing archives, attachments, extracted text, summaries, prompts, profiles, and run records remain intact.
- No OpenRouter model, output-token ceiling, prompt schema, or generation capability is reduced.
