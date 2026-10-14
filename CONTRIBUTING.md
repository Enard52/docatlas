# Contributing to DocAtlas

Welcome. Documentation fixes, reproducible bug reports and small improvements are useful contributions.
You do not need a paid model account or embedding weights to start.

## First run

Follow the [quick start](docs/QUICKSTART.md), including the sample document and question.
If a step is unclear, report the exact step and what you expected. Onboarding problems
are documentation problems we can improve together.

## Development setup

Fork the repository, clone your fork, and create a branch for one focused change.
Use Python 3.12 (the CI version), or Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -c requirements.lock -e '.[dev]'
python -m pytest -q
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python scripts/check_doc_links.py
```

On Windows, use `py -3 -m venv .venv` and `.venv\Scripts\Activate.ps1` in PowerShell.
Use `python` for the remaining commands once the environment is active.

Semantic retrieval is optional: install `'.[dev,semantic]'` with the same constraints.
The first real neural ingestion downloads weights. Unit tests use a deterministic
encoder and do not establish real semantic quality. Do not use private documents in tests.

## Where to work

| Area | Files |
|---|---|
| Extraction and chunking | `src/docatlas/ingest.py` |
| Storage and retrieval | `src/docatlas/store.py` |
| HTTP endpoints | `src/docatlas/api.py` |
| Provider configuration and answers | `src/docatlas/providers.py`, `src/docatlas/answer.py` |
| Browser workspace | `src/docatlas/static/` |
| Command line | `src/docatlas/cli.py` |
| Regression tests | `tests/` |
| Retrieval evaluation | `evals/`, `scripts/fetch_corpus.py` |

Start with the [good first issues](https://github.com/nymav/docatlas/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
Comment before a substantial change so maintainers can help narrow the scope.
These labels are for small, explained tasks, not every feature request.

## Pull requests

1. Explain the problem and the observable change. Link the issue if there is one.
2. Include a focused regression test for behavior changes; docs-only changes need a link check.
3. Run the checks above. Use `python -m ruff format src tests scripts` if formatting fails.
4. Include before/after screenshots for visible UI changes, using only sample data.
5. Wait for the existing CI tests and container smoke check. Explain anything you could not run.

Keep unrelated cleanup out of the PR. Do not commit `.env`, API keys, library databases,
user uploads, cached models or generated build files. See [security](SECURITY.md).
Benchmark/provider changes also need appropriate real-data evaluation; passing quote
checks is not proof that an answer is semantically correct.

Please follow the [code of conduct](CODE_OF_CONDUCT.md).
By contributing, you agree that your contribution is distributed under the [MIT license](LICENSE).
