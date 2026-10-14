# Quick start: ask a question and inspect its source

Requires Python 3.11+ (CI uses 3.12), SQLite with FTS5 and a modern browser.
No API key, paid service or embedding model is required for this walkthrough.

```bash
git clone https://github.com/nymav/docatlas.git
cd docatlas
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -c requirements.lock .
docatlas serve
```

Windows PowerShell: use `py -3 -m venv .venv` and `.venv\Scripts\Activate.ps1`
instead of the two environment commands above.

1. Open http://127.0.0.1:8000 in your browser.
2. In the library, upload [the sample document](../examples/team-handbook.md).
3. Wait for its ingestion job to finish, then ask **How long are release notes retained?**
4. Inspect the retrieved passage. The sample says release notes are retained for 90 days.
5. Inspect the source view and export the evidence if useful.

The default evidence mode returns source excerpts. It does not pretend to generate a
model answer. To generate a draft answer, use [Model setup](../README.md#connect-a-model).
Quotations are checked against the retrieved passages, but you must still review
whether they support the claim.

Prefer the CLI? Before starting the server, run:

```bash
docatlas ingest examples/team-handbook.md
docatlas serve
```

Use either route; the browser and CLI use the default `data/` workspace. Sample
content is fictional. Replace it with documents you are permitted to process.

## Common obstacles

- **Command not found:** activate `.venv` and repeat the installation command.
- **Port 8000 is in use:** run `docatlas serve --port 8001` and open that port instead.
- **Scanned or encrypted PDF:** provide unencrypted text-based files; OCR is a separate step.
- **Neural retrieval unavailable:** use the default BM25 mode first; install the
  semantic extra and configure `DOCATLAS_EMBEDDINGS=fastembed` before ingestion if needed.
- **Unexpected settings:** `.env` is not automatically loaded by the Python CLI.
  Docker Compose loads it. Saved provider settings take precedence over environment settings.
- **Unintended document replacement:** DocAtlas uses the filename as document identity.
  Uploading changed content under an existing filename (such as updating `handbook.md`)
  replaces that document's index rather than creating a separate source. To retain both
  documents as distinct search targets, give them distinct filenames (for example,
  `handbook-team-a.md` and `handbook-team-b.md`). Intentional updates should reuse the
  existing filename to update the index atomically. See
  [atomic ingestion in architecture](../README.md#architecture-and-decisions).

See [contributing](../CONTRIBUTING.md) for development checks, [operations](OPERATIONS.md)
for deployment, and [security](../SECURITY.md) before sharing a workspace.
