# Migration to v0.3.7

Version 0.3.7 adds the local **IDX Signal Desk** web GUI while retaining the v0.3.6 CLI.

## Upgrade

From the existing project root:

```bash
unzip -o ~/Downloads/idx_v037_complete_upgrade.zip -d .
chmod +x install_or_upgrade.sh
./install_or_upgrade.sh
python -m playwright install chromium
```

Verify:

```bash
python -c "import idx_digest; print(idx_digest.__version__)"
idx-digest --help
```

Expected version: `0.3.7`.

## Launch

```bash
idx-digest gui
```

The dashboard opens at `http://127.0.0.1:8787`. It starts pipeline runs in a background
thread, streams observability events to the page with SSE, and prevents overlapping runs.
All data remains under the existing `data/` directory.

## Compatibility

Existing `.env`, SQLite state, downloaded attachments, extracted text, cached summaries,
browser profile, logs, and traces remain compatible. CLI commands are unchanged.
