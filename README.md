# IDX Disclosure Digest

IDX Disclosure Digest collects IDX disclosure metadata and attachments, extracts their text, and builds isolated per-issuer research summaries through OpenRouter. It includes a CLI, a local browser-based workspace, durable SQLite checkpoints, recovery tools, and shareable exports.

## What it does

- Queries IDX announcements for an exact Jakarta-time interval.
- Supports all listed issuers or one ticker.
- Downloads PDF, XLSX, DOCX, HTML, and text attachments.
- Extracts native text first and uses Indonesian/English OCR for sparse PDF pages.
- Selects primary financial-statement sources and records every keep/skip decision.
- Deduplicates announcements, attachments, and already-completed model work.
- Produces document, announcement, and company-window summaries without mixing issuers.
- Saves files, summaries, prompts, audits, progress, and recovery state locally.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
cp .env.example .env
python verify_install.py
idx-digest gui
```

Add `OPENROUTER_API_KEY` to `.env` before using model-backed summaries. OCR also requires Tesseract and the Indonesian language pack.

See [INSTALLATION.md](INSTALLATION.md) for the complete macOS, Linux, Docker, upgrade, and troubleshooting instructions.

## Common commands

Run one issuer in an exact interval:

```bash
idx-digest run \
  --start '2026-08-05T21:00:00+07:00' \
  --end '2026-08-05T23:59:59+07:00' \
  --ticker ANTM
```

Run all listed issuers for whole dates:

```bash
idx-digest run --start 2026-08-01 --end 2026-08-05
```

Exercise download and extraction without model cost:

```bash
idx-digest run \
  --start 2026-08-05 \
  --end 2026-08-05 \
  --ticker ANTM \
  --skip-llm \
  --max-announcements 2
```

Open the local workspace without launching a browser automatically:

```bash
idx-digest gui --no-open-browser
```

Recover committed summaries without contacting IDX or OpenRouter:

```bash
idx-digest recover --start 2026-08-05 --end 2026-08-05
```

Finish company digests from cached announcement summaries:

```bash
idx-digest reduce-cached --start 2026-07-06 --end 2026-08-06
```

Preview financial-source refinement without model calls:

```bash
idx-digest refine-financials \
  --start 2026-07-06 \
  --end 2026-08-06 \
  --ticker PJAA \
  --dry-run
```

Export saved company summaries without model calls:

```bash
idx-digest export-all \
  --start 2026-07-06 \
  --end 2026-08-06 \
  --format md
```

Digests are saved per exact run window. `--start/--end` alone matches one saved window; add
`--range` to take every saved window overlapping those dates, or `--all-dates` to take every
saved window there is. Both keep one digest per ticker (its newest window) unless you pass
`--every-window`:

```bash
idx-digest export-all --range --start 2026-08-10 --end 2026-08-15 --format md
idx-digest export-all --all-dates --format md
```

The workspace's **Copy / export all** panel offers the same three choices — *Current scope*,
*Pick dates*, and *All saved dates* — over a list of every saved window. Picked dates preselect
the windows they touch; clicking a window includes or excludes it, which is the only way to
isolate one window when saved windows overlap.

Use `idx-digest --help` and `idx-digest COMMAND --help` for all options.

## Configuration

The checked-in `.env.example` documents every setting. The default provider policy pins the configured model to DeepInfra through OpenRouter and disables silent provider fallback:

```env
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=deepseek/deepseek-v4-flash-0731
OPENROUTER_PROVIDER=deepinfra
OPENROUTER_ALLOW_FALLBACKS=false
OPENROUTER_REQUIRE_PARAMETERS=true
```

The local GUI binds to `127.0.0.1` by default and has no authentication. Do not expose it to a public interface.

IDX transport defaults to `auto`: normal HTTP is tried first, then the same persistent Chromium session is used for protected metadata or attachment requests. The browser flow does not bypass CAPTCHAs; complete any interactive verification in the visible browser window.

## Output

```text
data/
├── idx_digest.sqlite3
├── last_run.json
├── prompts.json
├── browser-profile/
├── logs/
├── traces/
├── runs/<RUN_ID>/
├── raw/<TICKER>/<ANNOUNCEMENT_ID>/
├── text/<TICKER>/<SHA256>.txt
├── share/
└── companies/<TICKER>/
    ├── announcements.jsonl
    └── latest_window_summary.json
```

Treat `data/` as private research material. It may contain disclosure text, exact prompts, model responses, source URLs, and browser session state.

## Design and operations

The system summarizes each attachment once, reduces attachment summaries into one announcement, and reduces announcement summaries into one company window. This keeps retries bounded and enforces issuer isolation.

Read [HOW_IT_WORKS.md](HOW_IT_WORKS.md) for the complete algorithm, caching rules, recovery semantics, and concurrency model. Release and migration history is consolidated in [CHANGELOG.md](CHANGELOG.md).

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest -q
python verify_install.py
```

The test suite is offline by default and should not incur OpenRouter cost.

## Operational cautions

- IDX website endpoints are not presented as a stable public API; retain response-shape and completeness checks.
- Use conservative request pacing and a descriptive user agent.
- Never commit `.env`, `data/`, cookies, or the browser profile.
- Treat extracted attachment content as untrusted input.
- Historical audit can trigger expensive per-ticker completeness recovery; normal incremental runs do not.
- A partial run keeps its checkpoints but does not claim successful metadata coverage.
