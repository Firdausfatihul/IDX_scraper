# v0.3.9: Durable overnight runs and recovery

v0.3.9 fixes the failure mode where an overnight GUI run could appear empty after an internet outage or server restart.

## What is durable now

- Every downloaded file, extracted text artifact, document summary, and announcement summary is committed immediately.
- A readable company export is refreshed after each announcement.
- GUI state and event history are written under `data/runs/<run-id>/` while the run is active.
- A restarted GUI marks an unfinished run as `interrupted`, reloads its checkpoints, and exposes a Resume button.
- The pipeline catches metadata/network interruption and continues to partial reducers and recovery export.
- Company reducer failures no longer hide saved announcement summaries.
- `idx-digest recover` exports committed summaries without internet or OpenRouter.
- Repeated openpyxl data-validation warnings are suppressed because they do not affect cell-value extraction.

## Recover an existing interrupted window

```bash
idx-digest recover \
  --start 2026-08-06 \
  --end 2026-08-06
```

Output is written to `data/recovery/<timestamp>/recovery.json`, with per-ticker announcement JSONL files.

## Resume

Open `idx-digest gui`. The latest interrupted or partial run is restored from disk. Click **Resume interrupted run**. The same request is replayed, while existing files, text, document summaries, and announcement summaries are reused from cache.

## Recover old v0.3.7/v0.3.8 work in the GUI

Enter the same start/end window and ticker, then click **Open cached checkpoints**. This performs no network call and displays any company or announcement summaries already committed in SQLite.
