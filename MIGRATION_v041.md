# v0.4.1: Shareable all-company exports

v0.4.1 adds a zero-LLM sharing layer for saved company-window digests.

## GUI

The Company digest header now includes **Copy / export all**.

The share panel supports:

- **Friend-ready**: overview, material changes, financial figures, corporate actions, expansion, management/control, capital structure, listing/regulatory events, analytical scenarios, risks, and items to monitor.
- **Signals only**: overview plus corporate-action/expansion/management/capital/regulatory/scenario/monitoring sections.
- **Everything**: includes timeline and limitations as well.
- Per-section checkboxes.
- **Copy all** to the clipboard.
- **Download Markdown** and **Download TXT**.

Company summaries are appended alphabetically by ticker. This operation never sends multiple companies to an LLM and never performs cross-company analysis.

## CLI

Create one combined Markdown file from the saved July 6 to August 6 window:

```bash
idx-digest export-all \
  --start 2026-07-06 \
  --end 2026-08-06 \
  --format md
```

Signals only:

```bash
idx-digest export-all \
  --start 2026-07-06 \
  --end 2026-08-06 \
  --signals-only \
  --format txt
```

Choose explicit sections:

```bash
idx-digest export-all \
  --start 2026-07-06 \
  --end 2026-08-06 \
  --sections overview,corporate_actions,expansion_projects,items_to_monitor
```

No OpenRouter or IDX request is made by `export-all`.

## Live combined files

Normal company reduction and `reduce-cached` now refresh friend-ready combined snapshots after every company commit:

```text
data/share/latest-all-companies.md
data/share/latest-all-companies.txt
```

For a one-ticker run the equivalent files are named with that ticker. Writes are atomic, so an interruption cannot leave a half-written combined file.

Completed GUI runs also expose run-specific `All companies MD` and `All companies TXT` artifacts.
