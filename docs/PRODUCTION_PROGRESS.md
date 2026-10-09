# Production implementation progress

Phase 2 is skipped at the user's request. This skips collection and review of a
real-work corpus, not the automated checks needed for code changes. Consequently
representative extraction/answer quality remains unverified; it cannot be recorded
as a passing production gate.

| Phase | Current state | Evidence or remaining work |
|---|---|---|
| 1: model setup | Implemented local path | Live LM Studio connection/format probe passed; local answer quality failed |
| 2: real-work corpus | Skipped by user | No representative human-reviewed corpus |
| 3: durable ingestion | Core implemented | Persistent queue, stages, process isolation, cancellation, bounded retries, restart recovery, pre-migration backup and atomic job/document commit tested |
| 4: mixed extraction | Partially implemented | DOCX paragraphs/tables, profile versioning and sparse-PDF-page warnings added; OCR, faithful PDF tables and representative extraction QA remain |
| 5: retrieval efficiency | Improvement implemented | BM25 fetches only ranked matches; 30-case development ranking unchanged; reranker/ANN/version grouping remain unvalidated |
| 6: semantic support | Open quality gate | Local Mistral failures remain; source-excerpt mode avoids generation, model drafts are optional and explicitly unverified |
| 7: usability | Core upload flow implemented | Persistent activity, errors/cancel/retry, filename filtering and source warnings checked in browser; answer/source Markdown export added; full accessibility and grouping remain |
| 8: resources/measurement | Core controls implemented | Single ingestion worker, 32-job bound, deadline/RSS monitoring, readiness index checks; real workload/GPU load testing remains |
| 9: release verification | Partially verified | 57 automated tests and wheel build; backup/restore and synthetic search load passed; local Docker engine unavailable |

## Operations measurements

An isolated local run created 500 short synthetic Markdown documents and issued
200 keyword queries at concurrency four. Every top result matched its unique
marker. Search p50 was 0.758 ms and p95 was 2.128 ms. A SQLite backup restored to
a fresh directory with the same document fingerprint and three identical query
results. This measures short synthetic keyword search, not PDF/embedding/model
performance, representative answer quality or an SLA.

The 30-question public documentation development regression retained BM25
recall@5 0.9583 and MRR@5 0.8090 after replacing the full-library read with a ranked
SQL query. The same known question failures remain. No new quality claim follows
from that regression. Profile-aware corpus fingerprints require baselines to be
regenerated/reviewed when extraction provenance changes.

## Browser verification

A public synthetic DOCX report was uploaded through the actual file picker. Its
job reached ready; source inspection preserved a table row as
`Database | PostgreSQL`. A deliberately invalid PDF reached failed with an explicit
extraction error. Filename filtering hid unrelated documents without hiding job
activity. An early filter-scope bug discovered in this test was fixed and rechecked. Markdown
export provides a selectable preview and download link; the in-app browser did not
report a file-download event, so direct download there is not counted as verified.

## Explicit limits

Process separation is not an OS security sandbox. The worker runs as the same
filesystem account and can access that account's files. RSS is sampled every half
second, so it is not a hard allocation limit; transient spikes may occur before
termination. Upload spooling currently buffers one bounded file in API memory
before writing its private source. Legacy synchronous ingestion/CLI routes remain
for compatibility and do not have the background worker's process limits.

Sources are private temporary files: successful, invalid and cancelled jobs remove
them; retryable failures retain them for up to seven days, swept on startup and
hourly. After expiry the user must upload again. Job metadata persists separately.
Do not run parallel API replicas or synchronous CLI updates against a live workspace;
only one ingestion worker may own a workspace. Linux/macOS are supported for the
OS lock and RSS monitoring. Network/tenant isolation requires additional work.

A release cannot be marked production ready while semantic support, mixed-format
quality, OCR requirements and deployment checks remain unresolved. The skipped
corpus phase means those quality gates have no representative acceptance evidence.
