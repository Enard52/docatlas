# Operating DocAtlas

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| DOCATLAS_DATA_DIR | data | SQLite database and embedding cache |
| DOCATLAS_EMBEDDINGS | bm25 | bm25 or fastembed |
| DOCATLAS_EMBEDDING_MODEL | BAAI/bge-small-en-v1.5 | English neural embedding model |
| DOCATLAS_LLM_URL | empty | Trusted OpenAI-compatible base URL |
| DOCATLAS_LLM_MODEL | empty | Model identifier at that endpoint |
| DOCATLAS_LLM_KEY | empty | Server-side provider credential |
| DOCATLAS_LLM_KIND | lmstudio | Local lmstudio, remote api, or disabled |
| DOCATLAS_LLM_FORMAT | json_schema | json_schema or json_object |
| DOCATLAS_LLM_TIMEOUT | 120 | Provider deadline in seconds, 10–300 |
| DOCATLAS_LLM_CONTEXT_TOKENS | 8192 | Match loaded model context |
| DOCATLAS_LLM_MAX_TOKENS | 1400 | Output limit; must leave input budget |
| DOCATLAS_API_KEY | empty | Workspace authentication; required for network binding |
| DOCATLAS_MAX_UPLOAD_MB | 10 | Per-file byte limit |
| DOCATLAS_RATE_LIMIT | 60 | Requests per minute per direct client IP |
| DOCATLAS_MIN_DENSE_SCORE | 0.55 | Heuristic semantic evidence threshold |

## API

All `/api/*` routes require `X-API-Key` when a workspace key is configured. The
public `/health` route returns only readiness of the SQLite connection. The UI shell
is public but cannot read protected documents without the key.

- `GET /api/config`: capabilities without secrets.
- `POST /api/documents`: multipart `file` and optional `source` URL. Does not fetch URL.
- `GET /api/documents`: library metadata.
- `GET /api/documents/{id}`: extracted passages.
- `DELETE /api/documents/{id}`: document and both search indexes.
- `POST /api/search`: `{question, mode, k, document_id?}`.
- `POST /api/ask`: same contract, optional UUID `operation_id`, plus generation.
- `DELETE /api/requests/{operation_id}`: cancel a pending/running answer.
- `GET /api/provider`, `PUT /api/provider`: masked settings/read or atomic save.
- `POST /api/provider/models`, `POST /api/provider/check`: model discovery/live probe.
  These four setup routes require a direct localhost client and localhost hostname.
- `POST /api/feedback`: `{request_id, helpful, note?}`; persisted locally.
- `GET /api/metrics`: process-local counters and last-1000-request p95.
- `GET /api/openapi.json`: full API schema.

Generation uses asynchronous HTTP with a configurable 10–300-second deadline,
four active operations and at most eight waiting operations per process. Waiting
for a slot is limited to 15 seconds; a full queue returns 503. No automatic retry
can duplicate paid model calls. Cancel closes DocAtlas's outgoing request and frees
its slot, but the upstream model may continue work already dispatched. Retrieval
and extraction still run in worker threads; cancelling an answer cannot kill a
running thread. These limits are process-local, not a durable distributed queue.
Provider responses and model lists are limited to 256 KB. Redirects and ambient
proxy settings are disabled for provider calls. Context uses a conservative UTF-8
byte budget, preserves complete passages, and is not an exact model tokenizer.

Saved `data/provider.json` takes precedence over environment settings; it is
written atomically with mode 0600. It contains a plaintext provider key if set;
never commit it or include it in shared backups. API responses and validation
errors omit the key. A changed endpoint does not inherit the old endpoint's key.
A passing probe is held in process memory and resets on restart or save. It means
that the connection and citation format passed, not that answer quality passed.

The first neural call includes model load/download latency. Warm evaluation excludes it.

## Persistence and backup

Use the SQLite backup API for a consistent live backup, or stop the application before
copying the data directory. Do not copy only the main database while WAL writes are
active. Verify backups by opening a restored database and querying known documents.
Original uploaded binaries are not retained; keep your source files to re-index.
Changing the chunker or model requires re-ingestion; unchanged-content detection
currently tracks content and embedding model, not a configurable chunker version.

Do not run multiple application replicas against the same mounted SQLite database.
For larger deployments, migrate storage, jobs, rate limiting and identity to external
services. Current metrics and rate limits reset on process restart. Behind a reverse
proxy the limiter sees that proxy's IP; enforce per-user limits at the proxy layer.

## Provider troubleshooting

`evidence_only` means no generation provider is configured. `generation_unavailable`
means the provider failed, returned incompatible JSON, or a claim failed quote/source
validation. `insufficient_evidence` means the retrieval heuristic abstained. None is
silently converted into a successful generated answer.

From Docker Desktop, a model server on the host commonly uses
`http://host.docker.internal:1234/v1`. Ensure the host model server accepts that
connection. Linux host networking may require an explicit host-gateway mapping.

## Known quality limitations

The development corpus contains technical terms that favor BM25. More diverse,
human-reviewed data is needed before choosing a retrieval strategy for another domain.
The abstention heuristic can mistake topical similarity for an answer, especially on
personal or time-sensitive questions. Quote verification does not verify entailment.
Text-based PDF extraction preserves page numbers but not layout; scanned PDFs need
external OCR. This release has no automatic reranker, OCR or graph index.
