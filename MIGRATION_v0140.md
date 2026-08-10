# v0.14.0 · Pipeline Observatory

v0.14.0 is Phase 4 of the throughput program and is cumulative over v0.10.0 through v0.13.0. It changes observability and tuning guidance only. Analytical source selection, routine triage rules, duplicate suppression, prompt schemas, ticker isolation, and recovery semantics are unchanged.

## What changes

- Global LLM scheduler telemetry now records peak active slots, peak queue depth, per-stage/ticker activity, average/max queue wait, and peak utilization.
- Bounded extraction telemetry now records peak workers, pending/inflight depth, queue wait, backpressure count/time, and peak utilization.
- Adaptive provider telemetry now records active/current slots, success/failure counts, average/EWMA/max latency, wait pressure, throttles, and ramp events.
- Final run reports include `performance` and `diagnostics.phase4_performance` with throughput, bottleneck classification, utilization, latency, triage savings, and conservative next-run tuning recommendations.
- The Desk adds a compact Pipeline Observatory for live slots, queue depth, throughput, ETA, and provider latency.
- The next-run tuning recommendation can be applied to GUI controls with one click. It never mutates a running job.

## Safety

The advisor cannot exceed GUI-supported limits and does not change analytical behavior. Live adaptive provider backoff remains controlled by Phase 3. The recommendation is calculated only after the run completes so benchmark conditions remain deterministic.

No database migration is required for v0.14.0.
