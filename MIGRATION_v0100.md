# v0.10.0 · Signal Desk Library

This is a substantial GUI/workspace release. The scraper, cache hierarchy, company isolation, smart attachment policy, recovery flow, Prompt Studio, sharing, and provenance inspector remain available.

## What changes

- The GUI is rebuilt as a modern-retro filing desk inspired by plain corporate-document sites: serif masthead, navy/red/purple document palette, horizontal rules, compact ledgers, and modern responsive navigation.
- **Desk** contains new-run controls, live pipeline work, current digest, recovery, cached reduction, financial refinement, sharing, artifacts, and ticker inspection.
- **Library** lists saved company-summary windows and persisted run history independently of the New Run date fields.
- **Companies** provides a permanent ticker index across all saved company windows in the active profile.
- **Activity** reopens saved `events.jsonl` streams after restart and can archive a run as a ZIP snapshot.
- **Research profiles** isolate SQLite data, runs, prompts, share exports, browser profile, and UI configuration. The existing `data/` directory automatically becomes **Main archive** and is not moved.
- New profiles start with zero runs and zero summaries. They may optionally copy the current run-form configuration and Prompt Studio configuration.
- Run-form settings, selected view, and theme auto-save to `profile_state.json` in the active profile.
- Run events and run state continue to persist continuously while work is in progress.

## Existing data

No migration or destructive database rewrite is required. Existing data remains at:

```text
data/idx_digest.sqlite3
data/runs/
data/raw/
data/text/
data/companies/
```

The new profile registry is stored at:

```text
data/profiles.json
```

New isolated profiles use:

```text
data/profiles/<PROFILE_ID>/
```

## Run snapshot

The **Save run snapshot** action creates a ZIP inside the run directory and returns it to the browser. It contains run state, the complete persisted stream JSONL, prompt snapshot, report/recovery/share files when present, profile-state snapshot, and run-level artifacts that live elsewhere inside the active profile.

## Privacy

The redesigned GUI contains no Google Analytics, Google Tag Manager, remote fonts, or external UI assets. It remains a local application served from `127.0.0.1` by default.
