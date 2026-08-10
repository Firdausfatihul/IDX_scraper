# Migration to v0.3.4

Version 0.3.4 adds timestamped diagnostics, progress bars, browser tracing, streamed
structured summaries, and per-stage slowdown reporting. It does not require a database
migration and preserves the v0.3.3 strict summary schemas.

## Upgrade an existing editable installation

Extract the patch into the existing project, then reinstall:

```bash
unzip -o idx_observability_v034_patch.zip -d .
pip install -e .
hash -r
python -c "import idx_digest; print(idx_digest.__version__)"
```

Expected version:

```text
0.3.4
```

## Full diagnostics run

```bash
idx-digest run \
  --start 2026-08-05 \
  --end 2026-08-05 \
  --ticker ANTM \
  --max-announcements 2 \
  --diagnostics
```

Live diagnostics are written to stderr. The final JSON report remains on stdout.
A timestamped JSONL log is created under `data/logs/`, and browser tracing creates a
Playwright trace ZIP under `data/traces/` whenever Chromium is used.

## Individual options

```text
--verbose, -v       Detailed timestamped stage/cache/retry logs
--trace-browser     Browser network logs and Playwright trace ZIP
--stream-summary    Token-by-token structured summary JSON
--progress          Live progress bars, enabled by default on interactive terminals
--no-progress       Disable progress bars
--log-file PATH     Choose the JSONL log destination
--diagnostics       Enable all of the above
```

No files under `data/` need to be deleted. Existing downloads, extracted text, browser
profiles, and valid summaries remain reusable.
