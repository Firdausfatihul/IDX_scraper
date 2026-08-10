# Migration to v0.3.8

Version 0.3.8 adds editable prompt profiles and a richer corporate-action/expansion schema.
It is a complete-source upgrade and preserves `.env`, SQLite, downloaded files, extracted text,
and the persistent Chromium profile.

## Upgrade

From the existing project directory:

```bash
unzip -o ~/Downloads/idx_v038_complete_upgrade.zip -d .
chmod +x install_or_upgrade.sh
./install_or_upgrade.sh
hash -r
```

Verify:

```bash
python -c "import idx_digest; print(idx_digest.__version__)"
```

Expected:

```text
0.3.8
```

Launch:

```bash
idx-digest gui
```

## Prompt Studio

Select **Prompts** in the GUI top bar. The five editable layers are:

- system guardrails;
- document analysis;
- long-document merge;
- announcement reducer;
- company-window digest.

Saved prompts are written to `data/prompts.json`. They are local and are not embedded in `.env`.
The GUI refuses prompt edits while a run is active so one run cannot use a half-old, half-new bundle.

Each prompt uses documented template variables. The editor validates unknown variables and requires
the source-bearing variables, such as `{document_text}` or `{announcements_json}`, before saving.

## Cache behavior

Old summaries remain physically safe, but v0.3.8 accepts a cached summary only when:

- the configured model/provider identifier matches; and
- the relevant system/layer prompt hash matches; and
- the v0.3.8 structured-output schema version matches.

A mismatch removes only the stale summary row. Downloaded attachments and extracted text remain.

## New analysis fields

The strict schemas add dedicated sections for:

- expansion projects and capex;
- management or control changes;
- capital-structure and ownership events;
- listing or regulatory events;
- structured analytical scenarios.

Each analytical scenario includes classification, topic, analysis, basis, assumptions, confidence,
and caveats. The allowed classifications are `explicit_fact`, `derived_calculation`, and
`analyst_hypothesis`.

## Default corporate-action profile

The default announcement prompt includes few-shot classification examples based on the requested
GTSI, MEJA, ALMI, ENVY, TPIA, UNVR, BUKK, and SAFE patterns. They are explicitly marked as examples
only. Names, numbers, and conclusions must never be transferred to another issuer unless supported
by that issuer's own source documents.
