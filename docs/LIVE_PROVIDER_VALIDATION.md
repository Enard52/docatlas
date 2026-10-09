# Live LM Studio validation

The installed `mistral-7b-instruct-v0.1` model was tested using localhost LM Studio
0.3.16 with a loaded 4096-token context. The server was verified listening on
127.0.0.1 only. No private work documents or paid APIs were used. Tests used the
public repository README in a temporary library. These are developer integration
checks, not an independent benchmark.

## Observed results

Both runs discovered models and passed the real generation/citation-format probe.
The probe took approximately 4.3–4.7 seconds. Four development questions were then
asked. Answers below were manually inspected during implementation.

| Question | First run: 512 output tokens | Second run: 1024 output tokens, shorter-answer prompt |
|---|---|---|
| Supported retrieval methods | Output truncated; correctly rejected | Returned unrelated claims about filenames and a model server; quality failure |
| Does quote validation prove entailment? | Correct negative claim with matching quote | Correct negative claim with matching quote |
| Can the app work without a configured model? | Affirmative claim cited a configuration example, not supporting evidence | Affirmative claim cited the quote-check requirement, not supporting evidence |
| Repository owner's private home address | Retrieval abstained | Retrieval abstained |

Second-run answer latencies were approximately 28.2, 9.8, 8.7 and 0.009 seconds.
Those are individual observations, not p95 measurements or a capacity commitment.
No aggregate accuracy percentage is claimed for these four authored questions.

## Interpretation and changes

Connection and JSON/quote validation work with this model. Semantic answer quality
failed: existing source/quote checks accepted unsupported and irrelevant claims.
Increasing output size and refining the prompt did **not** resolve the failure.
Generated output is therefore labeled a draft with quote checks only. The API
exposes `verification.semantic_support: "not_verified"`; `status: "answered"` is a
legacy delivery status, not a correctness certification.

A stronger model, improved context selection, and a separately evaluated support
policy may help. They must be validated on reviewed work documents; this report
cannot establish which remedy will pass. Current model quality blocks a production
answer-accuracy claim. See Phase 6 of the production plan.

Local machine-readable reports are ignored files under `data/`. They are retained
for inspection and must not be silently rewritten into passing results. The smoke
script's exit code is an integration check only: a quoted answer can be wrong.
