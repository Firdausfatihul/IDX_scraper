# v0.3.3 summary-schema repair

From the existing project directory:

```bash
unzip -o ~/Downloads/idx_summary_schema_fix_v033.zip -d .
pip install -e .
hash -r
python -c "import idx_digest; print(idx_digest.__version__)"
```

Expected version: `0.3.3`.

Keep the existing `.env`, `data/`, SQLite database, downloaded documents, and browser profile.
Then run the same command again. Invalid cached `{}` and `{"": ""}` summaries are detected
and regenerated automatically.
