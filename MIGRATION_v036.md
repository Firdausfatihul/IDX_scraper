# Migration to v0.3.6

Version 0.3.6 fixes terminal spam and display collisions.

## Changed defaults

- `--diagnostics` no longer enables raw summary streaming.
- Browser asset requests are saved in the Playwright trace, not printed.
- Cache hits and per-page extraction events are kept in JSONL, not printed.
- Completed progress rows are removed and the dashboard clears at the end.
- The slowdown report is limited to the five slowest stages.
- `--stream-summary` automatically disables progress bars.

## Explicit noisy modes

```bash
--browser-network
--cache-logs
--page-logs
--stream-summary
```

These options are intentionally opt-in.
