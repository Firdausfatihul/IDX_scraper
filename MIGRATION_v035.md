# Migration to v0.3.5

Version 0.3.5 parallelizes independent OpenRouter document-summary calls within each IDX announcement.

## Upgrade

```bash
unzip -o ~/Downloads/idx_parallel_v035_patch.zip -d .
python -m pip install -e .
hash -r
python -c "import idx_digest; print(idx_digest.__version__)"
```

Expected version: `0.3.5`.

No database reset is required. Existing downloads, extracted text, valid document summaries, announcement summaries, and the persistent browser profile remain reusable.

## Configure concurrency

Add to `.env`:

```env
LLM_CONCURRENCY=2
```

Or override it for one run:

```bash
idx-digest run \
  --start 2026-08-05 \
  --end 2026-08-05 \
  --ticker ANTM \
  --max-announcements 2 \
  --llm-concurrency 2 \
  --diagnostics
```

Use `1` to restore sequential document summaries. Values from 2 to 4 are practical for most runs. Higher values can trigger provider RPM or TPM rate limits.

## Concurrency boundary

The following remain serial by design:

- IDX metadata retrieval and persistent Chromium operations;
- attachment downloads through the shared Playwright browser context;
- text extraction and OCR preparation;
- announcement-level summaries;
- company-window summaries.

Only independent document-summary calls run concurrently. SQLite writes occur in the main thread as each worker completes.

When multiple document summaries run concurrently, their raw token streams are disabled to avoid interleaved JSON in the terminal. Timestamped request/completion logs and progress remain visible. Announcement and company-summary streaming remain available.
