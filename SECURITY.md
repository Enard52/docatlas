# Security model

DocAtlas is intended for one trusted workspace on localhost or behind an authenticated
TLS reverse proxy. A shared workspace API key grants access to all its documents and
write operations. It does not provide user-level ACLs or tenant isolation.

Implemented controls: constant-time key comparison, no secrets in browser configuration,
same-origin writes, content-size limits, fixed file types, no arbitrary URL fetching,
parameterized SQL, bounded provider output, no agent tools, exact quote/source checks,
HTML rendered as text, restrictive Content Security Policy, and process-local limits.
Uploads require Content-Length. PDFs must be unencrypted and at most 300 pages.

Threat boundaries: retrieved documents are untrusted content; the model never receives
tools or permission to execute instructions from them. However prompt injection can
still influence answer wording, and quote matching is not an entailment proof. Always
inspect evidence for important decisions. Treat generated answers as untrusted output.

Upload parsers execute in-process. Byte/page/text limits do not fully prevent malicious
PDF decompression or parser-resource attacks; accept documents from trusted workspace
members, run inside memory-limited containers, and use a sandboxed ingestion worker
before accepting arbitrary internet uploads. Localhost binding alone is not an
authentication boundary on a shared machine; set a workspace API key there too.

Source content remains in SQLite; generation sends selected passages to the configured
provider. Feedback notes are stored locally. Request logs avoid document bodies and
provider credentials. Do not commit `.env`, database files, model caches or user uploads.
Rotate the workspace key by updating the environment and restarting the server.

Report security issues privately to the repository owner rather than uploading private
documents, credentials or exploit results in a public issue.

## Provider setup and local secrets

Model configuration routes require a direct loopback client and local hostname,
in addition to the workspace key when set. Provider settings are written atomically
to ignored `data/provider.json` with mode 0600. A provider key is plaintext in that
private file; protect the filesystem account and backup permissions. It is never
returned by configuration or validation responses and does not transfer to a
changed endpoint. This is not an encrypted secret vault.

Provider calls do not follow redirects or ambient proxy configuration. Local mode
restricts the hostname to loopback or Docker's host alias; remote mode requires
HTTPS. Owner configuration is a trusted capability, not a general-purpose URL
fetch endpoint. DNS/private-network egress controls are not a SaaS security boundary.
Do not expose this single-user design as a multi-tenant service.

Source/quote validation establishes that quoted text was supplied to the model.
It does not establish semantic support, truth or immunity to prompt injection.
Live local tests demonstrated unsupported claims with valid quotations. Generated
responses are labeled drafts and need evidence inspection.

## Ingestion process boundaries

Background uploads use private 0600 spool files under a 0700 directory, a persistent
SQLite queue, and a single isolated worker process. The parent owns cancellation,
wall-time limits and sampled RSS limits. Isolation prevents parser work from sharing
the API process; it does not sandbox filesystem/network access or contain a library
exploit. Workers run as the same OS account. Use trusted local work documents, keep
the deployment local, and apply OS/container restrictions before accepting arbitrary
untrusted uploads from other users. Original source files for retryable failures
remain private for up to seven days; completed/invalid/cancelled sources are removed.
The worker receives no generation/workspace key environment variables, but its
filesystem permissions still permit access to that account's data directory.
