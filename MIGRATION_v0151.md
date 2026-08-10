# v0.15.1 IDX Throttle Hotfix

This patch fixes the metadata fallback behavior exposed by an IDX HTTP 429 run.

## Changes

- HTTP 429 is no longer treated as a generic browser-verification loop with a fixed 2-second retry.
- Browser metadata requests now honor a numeric `Retry-After` response header when available.
- Without `Retry-After`, IDX 429 retries use exponential cooldown plus jitter, bounded by the existing browser verification deadline.
- A daily shard that still cannot paginate completely may use the stock-master per-ticker fallback, but ticker requests are paced and receive periodic burst rests instead of firing as fast as Chromium can execute them.
- The GUI shows `IDX RATE LIMITED` with a live cooldown countdown during 429 recovery.
- `verify_install.py` now verifies required prompt names instead of assuming a fixed prompt count.

## Safety

No existing database table is removed or rewritten. No data directory is shipped in the upgrade overlay.

## LLM generation limits

v0.15.1 does **not** reduce or modify OpenRouter output-token ceilings. This patch changes only IDX metadata pacing, 429 recovery, GUI telemetry, and install verification.
