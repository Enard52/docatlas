# DocAtlas production work plan

Target: a local, single-user workspace for mixed work documents: PDFs, reports,
notes and documentation. LM Studio is the primary answer provider; remote APIs
are optional and must be explicitly selected. No timeline is implied by these
phases. Each phase ends with evidence that its acceptance checks passed.

## Current release status

Production readiness is **not established**. The 30 AI-authored development
questions test a narrow public documentation corpus. They are not a user quota,
not representative of mixed work documents, and not an answer-accuracy benchmark.
The product has no 30-question cap. Existing rate limiting is a separate per-minute
operational control.

Implemented foundation: persistent atomic ingestion, duplicate detection, text
PDF/Markdown/TXT/HTML extraction, BM25 and optional neural/hybrid retrieval, quote
and source validation, library/source inspection, feedback, local model setup,
provider discovery/testing, cancellation and bounded pending answer work.

Unfinished release gates: robust mixed-document extraction, durable ingestion,
representative reviewed evaluation, claim-support quality, measured performance,
recovery drills and deployment verification. A successful model connection does
not close any of those gates.

## Phase 1 — Connect and control the real model

Why: users need to configure their own model without editing source or mistaking
saved settings for a working connection.

1. Load a chat model in LM Studio. Read its loaded context from the model settings.
   Keep the server restricted to localhost. Start it on port 1234.
2. In Model setup, choose LM Studio and enter `http://127.0.0.1:1234/v1`. Discover
   models and select a chat model, not an embedding model.
3. Set the loaded context, output tokens and timeout. Save settings. The saved
   file must be private, atomic, excluded from Git and masked in every response.
4. Run the live probe. Verify that its expected answer and exact quote pass.
   Display the result as a connection test, never a quality certification.
5. For optional APIs, require an explicit provider change and show that selected
   passages leave the computer. Keep secrets server-side and never carry a key
   to a different endpoint.
6. Exercise stopped server, invalid model, wrong key, unsupported JSON format,
   malformed response, truncated output and cancellation. Preserve retrieved
   evidence when generation fails. Bound pending requests and response sizes.

Implemented and tested for local setup. Remote contracts have mock coverage;
there is no live remote-provider verification or spend authorization. Local live
answer-quality failures remain documented separately.

## Phase 2 — Establish a real mixed-document corpus

Why: a documentation-only fixture cannot reveal failures in reports, messy notes,
scanned pages or conflicting versions.

1. Create a private corpus manifest outside Git. Record a stable document ID,
   source filename, format, language, owner approval, version, and sensitivity.
   Use local paths rather than copying document text into a public issue.
2. Include searchable PDFs, scanned PDFs, table-heavy reports, short notes,
   long documentation, duplicate filenames, old/current versions and corrupt
   files. Include DOCX if work reports arrive in that format.
3. Keep representative sizes and layouts. Do not replace difficult pages with
   clean synthetic text. Strip unnecessary personal data from test copies.
4. Inspect each document manually: identify pages/headings that should survive
   extraction and note known tables or diagrams that require special handling.
5. Freeze a corpus revision using hashes. Keep a separate unseen evaluation set
   so tuning cannot silently turn the test set into development data.

Acceptance: every supported format has a reviewed example and every unsupported
format produces a clear message. Access to private work documents must come from
the user; public fixtures cannot stand in for this validation.

## Phase 3 — Make ingestion durable and recoverable

Why: long extractions must survive page refreshes and show progress rather than
blocking an upload with no explanation.

1. Add a migrations table and versioned schema. Before migration, make a SQLite
   backup using the backup API. Verify a restore before changing production data.
2. Add jobs with ID, document identity, content hash, stage, timestamps, attempt
   count and a safe error code. Stages: uploaded, queued, extracting, chunking,
   embedding, committing, ready, failed and cancelled.
3. Stream bytes into a private temporary file while counting actual bytes.
   Reject oversized content even when a client supplies misleading headers.
   Retain source binaries only under an explicit retention policy.
4. Return 202 with a job ID. Poll a status endpoint from the library and show
   filename, stage and retry/cancel actions. Progress must reflect observed
   stages; never invent percentages for opaque parser/model operations.
5. Run extraction in a separate process with wall-time and memory limits. Killing
   a parser process must not kill the API or leave a transaction half-written.
6. Persist intermediate work safely. Commit the document, chunks and both indexes
   together only when all required stages succeed. Keep the previous ready
   document usable if a replacement fails.
7. On restart, reconcile interrupted jobs. Requeue safe stages, mark uncertain
   stages explicitly, and avoid duplicate embeddings/index entries.
8. Retry transient failures with a bounded attempt policy. A corrupt PDF needs
   correction, not repeated retries. Show the distinction to the user.
9. Test restart during extraction, cancellation during embedding, full disk,
   duplicate upload, same-name replacement and failure before commit.

Acceptance: accepted jobs are never silently lost, failed replacements preserve
old search results, and restart/retry produces exactly one consistent index.
Current synchronous uploads do not meet this gate.

## Phase 4 — Extract mixed formats faithfully

Why: accurate retrieval starts with readable text and reliable source locations.

1. Measure text coverage per PDF page. Flag near-empty or image-only pages;
   do not treat a partly scanned report as fully extracted.
2. Add opt-in local OCR in the isolated ingestion worker. Choose and validate an
   installed OCR engine; avoid an automatic external service. Store OCR origin,
   page number and extraction warnings alongside chunks.
3. Extract DOCX paragraphs, headings and tables with decompression/size limits.
   Reject encrypted, corrupt or unsupported files with actionable messages.
4. Preserve table row/column context. Keep headers with rows rather than
   flattening unrelated cells into a misleading sentence. Label uncertain
   layout and require source inspection for ambiguous data.
5. Normalize repeated headers/footers and line-wrap artifacts while retaining
   the original page reference. Keep a versioned extraction/chunking profile.
6. Split by headings and semantic boundaries, respecting maximum passage size.
   Avoid sentence fragments and duplicated overlap in displayed answers.
7. Compare extracted text with manually identified source pages from Phase 2.
   Record missing sections, merged columns, table mistakes and OCR mistakes.

Acceptance: reviewed text and source locations match for the representative
corpus; unsupported visual content is clearly labeled instead of inferred.

## Phase 5 — Improve retrieval with measured tradeoffs

Why: installing more components is useful only when they improve relevant evidence
without unacceptable latency or resource use.

1. Write real questions with supporting document/page/span labels before tuning.
   Include jargon, paraphrases, short notes and questions whose answer is absent.
2. Measure BM25, dense and hybrid on the same frozen split. Report recall and
   ranking separately from generated-answer correctness.
3. Add metadata/version filters and exclude obsolete versions when the user
   explicitly chooses current material. Show source dates where available.
4. Test a local reranker on retrieved candidates. Bound its batch size and memory.
   Keep it only if it improves reviewed relevance at an acceptable latency.
5. Measure exact-vector scan time as the corpus grows. Introduce an ANN index
   only after measurement identifies a bottleneck; validate updates and deletions
   against the source of truth so stale vectors cannot leak into answers.
6. Cache embeddings by content hash plus embedding/chunker version. Invalidate
   safely when any of those change. Never reuse incompatible vectors.

Acceptance: measured improvement on an unseen split, no lost deletion/version
consistency, and documented latency/memory tradeoffs. Synthetic recall alone
cannot close this phase.

## Phase 6 — Validate what claims mean

Why: a real quotation can still fail to support the sentence that cites it. A live
Mistral smoke test demonstrated this failure in the current product.

1. Build a claim-level rubric: supported, partially supported, unsupported,
   contradicted, outdated, or unanswerable. Review the exact quote and source
   context rather than only whether the quote exists.
2. Keep deterministic source/quote checks as a first filter. They must reject
   invented IDs, quotes, malformed JSON and truncated responses.
3. Evaluate a separate verifier or extractive response path against reviewed
   labels. A second LLM's agreement is not proof. Measure verifier false accepts
   and false rejects, especially on negation, numbers and unrelated examples.
4. Suppress or clearly mark claims that fail the selected support policy. If no
   supported claims remain, return evidence or abstain. Never relabel a model
   or retrieval failure as a successful answer.
5. Test prompt injection in documents. Keep documents untrusted, no tool execution,
   no automatic network actions and no hidden remote fallback.
6. Keep user-visible qualification that semantic correctness requires inspection
   until a measured support gate exists. Do not call quote checks fact checking.

Acceptance: human-reviewed claim accuracy and abstention targets agreed for the
workload, including adversarial examples and an unseen split. Targets must be
set before examining final test results.

## Phase 7 — Make the workspace usable and accessible

1. Show ingestion jobs, errors, retries, warnings and readiness in the library.
   A filename listed as uploaded must not imply it is searchable.
2. Add document grouping, version labels, bulk selection and clear filters.
   Confirm destructive operations with the exact document names and consequences.
3. Link citations directly to the relevant page/section. Show warnings about OCR
   or layout beside evidence rather than hiding them in operational logs.
4. Preserve selected filters and make keyboard focus, labels, status announcements
   and narrow-screen layouts usable. Test keyboard-only navigation and screen
   reader announcements for long-running tasks and errors.
5. Add exportable answers with citations and provider/model metadata. Avoid
   exposing workspace keys or private documents in shared links.
6. Make feedback actionable: capture the relevant request and failure category
   under a documented retention policy, with opt-in text collection.

Acceptance: a user can add a mixed corpus, resolve a failed job, find a source,
inspect an answer and change models without using a terminal.

## Phase 8 — Measure performance and control resources

1. Record separate extraction, embedding, retrieval, generation and queue times.
   Add bounded counters for failures, truncation, cancellation and saturation.
   Avoid logging document text, questions or keys by default.
2. Establish cold/warm baselines on this machine with corpus sizes and loaded
   model settings recorded. Measure p50/p95 and memory under realistic concurrency.
3. Test overload: queue limits must respond quickly and recovered capacity must
   return after timeout/cancellation. Verify parser work cannot starve API health.
4. Tune concurrency for local GPU/CPU memory; four model requests may be too many
   for the installed hardware. Separate ingestion and generation resource pools.
5. Add explicit readiness checks for schema/index state. Keep model readiness
   separate so evidence search can remain available while LM Studio is stopped.

Acceptance: repeatable load results, bounded work, documented machine-specific
capacity and no unsupported SLA claims.

## Phase 9 — Prove recovery, security and release quality

1. Restore a live SQLite backup into a fresh data directory; compare document
   hashes, counts and known query results. Store provider secrets separately.
2. Test upgrade/migration failure and rollback. Validate that old documents remain
   usable after an interrupted update and that incompatible index versions warn.
3. Verify same-origin controls, authorization, key masking, upload limits, local
   setup restrictions, cancellation and response limits in automated tests.
4. Keep shared-key scope explicit. A multi-user release requires identities,
   per-document authorization and isolation tested for retrieval and citations.
5. Run the reviewed retrieval/claim evaluation, isolated parser tests, browser
   accessibility checks and load/recovery tests from a clean install.
6. Publish actual results with model/corpus/build revisions and remaining limits.
   Tag a release only when its agreed gates pass. Preserve each real implementation
   increment as a meaningful commit; never fabricate contribution dates.

Acceptance: clean installation, reviewed quality evidence, recovery drill and
measured capacity all pass for the stated single-user deployment. Only then label
that deployment production ready. Broader SaaS claims require additional gates.
