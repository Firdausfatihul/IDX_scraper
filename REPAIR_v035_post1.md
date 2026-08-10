# v0.3.5.post1 complete-source repair

This repair contains the complete `src/idx_digest` package. It restores modules such as `db.py`, `downloader.py`, `extractors.py`, `idx_client.py`, `browser_transport.py`, and `timeutils.py` that are dependencies of the parallel v0.3.5 pipeline.

Apply over an existing v0.3.4/v0.3.5 project, reinstall editable mode, and verify imports. Existing `.env`, `data/`, SQLite, browser profile, downloads, and summaries are preserved.
