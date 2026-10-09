import asyncio
import logging
import secrets
import threading
import time
import uuid
from collections import Counter, deque
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .answer import ModelAbstention, ProviderError, answer_async, generate_async
from .config import Settings
from .jobs import JobQueue
from .providers import ProviderFailure, ProviderInput, ProviderManager
from .retrieval import LocalEncoder
from .store import Store

logger = logging.getLogger("docatlas")


class Query(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    mode: Literal["bm25", "dense", "hybrid"] = "bm25"
    k: int = Field(default=6, ge=1, le=15)
    operation_id: uuid.UUID | None = None
    answer_style: Literal["evidence", "draft"] = "draft"
    document_id: str | None = Field(default=None, max_length=64)


class Feedback(BaseModel):
    request_id: uuid.UUID
    helpful: bool
    note: str = Field(default="", max_length=2000)


def create_app(settings=None, encoder=None):
    settings = settings or Settings.from_env()
    if settings.embeddings not in {"bm25", "fastembed"}:
        raise ValueError("DOCATLAS_EMBEDDINGS must be bm25 or fastembed.")
    if settings.embeddings == "fastembed" and encoder is None:
        encoder = LocalEncoder(settings.embedding_model, settings.data_dir / "models")
    store = Store(settings.data_dir, encoder)
    jobs = JobQueue(store, settings)

    @asynccontextmanager
    async def lifespan(app):
        jobs.start()
        try:
            yield
        finally:
            await asyncio.to_thread(jobs.close)

    app = FastAPI(
        title="DocAtlas",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.jobs = jobs
    app.state.store = store
    app.state.settings = settings
    providers = ProviderManager(settings)
    app.state.providers = providers
    active_asks = {}
    pending_work = 0
    counters = Counter()
    durations = deque(maxlen=1000)
    windows = {}
    state_lock = threading.Lock()
    work_slots = asyncio.Semaphore(4)

    @asynccontextmanager
    async def work_slot():
        nonlocal pending_work
        if pending_work >= 12:
            raise HTTPException(
                503, "Workspace is busy. Try again shortly.", headers={"Retry-After": "5"}
            )
        pending_work += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(work_slots.acquire(), timeout=15)
                acquired = True
            except TimeoutError as exc:
                raise HTTPException(503, "Work queue timed out. Try again shortly.") from exc
            yield
        finally:
            pending_work -= 1
            if acquired:
                work_slots.release()

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Validation errors must not echo provider keys submitted in the request.
        return JSONResponse(
            {
                "detail": [
                    {"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()
                ]
            },
            status_code=422,
        )

    @app.middleware("http")
    async def controls(request: Request, call_next):
        request_id = str(uuid.uuid4())
        start = time.perf_counter()
        if request.url.path.startswith("/api/"):
            if settings.api_key and not secrets.compare_digest(
                request.headers.get("x-api-key", ""), settings.api_key
            ):
                return JSONResponse(
                    {"detail": "A valid workspace API key is required."}, status_code=401
                )
            # Do not trust forwarded headers from arbitrary clients.
            client = request.client.host if request.client else "unknown"
            now = time.monotonic()
            with state_lock:
                for key in list(windows):
                    if not windows[key] or windows[key][-1] <= now - 60:
                        del windows[key]
                window = windows.setdefault(client, deque())
                while window and window[0] <= now - 60:
                    window.popleft()
                if len(window) >= settings.rate_limit:
                    return JSONResponse(
                        {"detail": "Rate limit reached. Try again shortly."},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )
                window.append(now)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                # Same-origin web UI; API callers without Origin may use API keys.
                origin = request.headers.get("origin")
                if origin and origin != str(request.base_url).rstrip("/"):
                    return JSONResponse(
                        {"detail": "Cross-origin writes are not accepted."}, status_code=403
                    )
            raw_length = request.headers.get("content-length", "0")
            if (
                request.url.path in {"/api/documents", "/api/ingestion/jobs"}
                and request.method == "POST"
                and "content-length" not in request.headers
            ):
                return JSONResponse({"detail": "Uploads require Content-Length."}, status_code=411)
            try:
                if int(raw_length) > (settings.max_upload_mb + 1) * 1024 * 1024:
                    return JSONResponse({"detail": "Request too large."}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_failed id=%s", request_id)
            response = JSONResponse(
                {"detail": "Unexpected server error.", "request_id": request_id}, status_code=500
            )
        elapsed = (time.perf_counter() - start) * 1000
        with state_lock:
            counters[f"http_{response.status_code}"] += 1
            durations.append(elapsed)
        response.headers.update(
            {
                "X-Request-ID": request_id,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        with store.connect() as db:
            db.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        return {
            "version": "1.0.0",
            "modes": ["bm25", "dense", "hybrid"] if encoder else ["bm25"],
            "default_mode": "hybrid" if encoder else "bm25",
            "generation": providers.public()["configured"],
            "provider": {
                k: v
                for k, v in providers.public().items()
                if k in {"kind", "model", "configured", "last_check", "privacy"}
            },
            "embedding_model": encoder.name if encoder else None,
            "max_upload_mb": settings.max_upload_mb,
        }

    def owner_only(request):
        # A shared library key is not authority to change a remote provider or read its settings.
        # Configuration is restricted to a client connected directly from the local machine.
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1", "testserver", "test"}:
            raise HTTPException(403, "Model setup requires a local hostname.")
        if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "Model setup is available only from this computer.")

    @app.get("/api/provider")
    def provider_config(request: Request):
        owner_only(request)
        return providers.public()

    @app.put("/api/provider")
    def save_provider(value: ProviderInput, request: Request):
        owner_only(request)
        return providers.save(value)

    @app.post("/api/provider/models")
    async def provider_models(value: ProviderInput, request: Request):
        owner_only(request)
        try:
            async with work_slot():
                return {"models": await providers.models(value)}
        except ProviderFailure as exc:
            return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=502)

    @app.post("/api/provider/check")
    async def provider_check(value: ProviderInput, request: Request):
        owner_only(request)
        value = providers.resolved(value)
        if value.kind == "disabled" or not value.model:
            raise HTTPException(422, "Select a model before testing generation.")
        snapshot = replace(
            settings,
            llm_url=value.base_url,
            llm_model=value.model,
            llm_key=value.api_key or "",
            llm_kind=value.kind,
            llm_format=value.response_format,
            llm_timeout=value.timeout_seconds,
            llm_context_tokens=value.context_tokens,
            llm_max_tokens=value.max_output_tokens,
        )
        probe = [
            {
                "id": "connection-probe",
                "text": "The DocAtlas connection test uses the code word atlas-ready.",
            }
        ]
        start = time.perf_counter()
        try:
            async with work_slot():
                claims, usage = await asyncio.wait_for(
                    generate_async(
                        "What code word does the DocAtlas connection test use?", probe, snapshot
                    ),
                    timeout=value.timeout_seconds,
                )
            if not any("atlas-ready" in c["text"] for c in claims):
                raise ProviderError(
                    "The model did not answer the connection test correctly.", "probe_failed"
                )
            check = {
                "status": "ready",
                "model": value.model,
                "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                "checked_at": datetime.now(UTC).isoformat(),
                "message": "Live generation and citation-format checks passed. Answer quality still requires evaluation.",
            }
            with providers.lock:
                current = providers.snapshot()
                if (
                    current.llm_url,
                    current.llm_model,
                    current.llm_format,
                    current.llm_key,
                    current.llm_timeout,
                    current.llm_context_tokens,
                    current.llm_max_tokens,
                ) == (
                    snapshot.llm_url,
                    snapshot.llm_model,
                    snapshot.llm_format,
                    snapshot.llm_key,
                    snapshot.llm_timeout,
                    snapshot.llm_context_tokens,
                    snapshot.llm_max_tokens,
                ):
                    providers.last_check = check
            return {**check, "usage": usage}
        except TimeoutError:
            return JSONResponse(
                {
                    "detail": "The connection test timed out. Load the model and try again.",
                    "code": "provider_timeout",
                },
                status_code=504,
            )
        except ProviderError as exc:
            return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=502)
        except ModelAbstention:
            return JSONResponse(
                {"detail": "The model abstained on the connection test.", "code": "probe_failed"},
                status_code=502,
            )

    @app.get("/api/documents")
    def documents():
        return {"documents": store.list_documents(), "fingerprint": store.fingerprint()}

    @app.get("/api/documents/{document_id}")
    def document(document_id: str):
        result = store.document(document_id)
        if not result:
            raise HTTPException(404, "Document not found.")
        return result

    @app.post("/api/documents", status_code=201)
    async def upload(file: Annotated[UploadFile, File()], source: Annotated[str, Form()] = ""):
        limit = settings.max_upload_mb * 1024 * 1024
        data = await file.read(limit + 1)
        await file.close()
        if len(data) > limit:
            raise HTTPException(413, "File exceeds the upload limit.")
        if len(source) > 2000:
            raise HTTPException(422, "Source URL is too long.")
        async with work_slot():
            try:
                return await asyncio.to_thread(store.ingest, file.filename or "", data, source)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @app.post("/api/ingestion/jobs", status_code=202)
    async def enqueue(file: Annotated[UploadFile, File()], source: Annotated[str, Form()] = ""):
        limit = settings.max_upload_mb * 1024 * 1024
        try:
            data = bytearray()
            while part := await file.read(min(65536, limit + 1 - len(data))):
                data.extend(part)
                if len(data) > limit:
                    raise HTTPException(413, "File exceeds the upload limit.")
            return await asyncio.to_thread(jobs.submit, file.filename or "", bytes(data), source)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            await file.close()

    @app.get("/api/ingestion/jobs")
    def ingestion_jobs():
        return {"jobs": jobs.list()}

    @app.get("/api/ingestion/jobs/{job_id}")
    def ingestion_job(job_id: uuid.UUID):
        job = jobs.get(str(job_id))
        if not job:
            raise HTTPException(404, "Ingestion job not found.")
        return job

    @app.delete("/api/ingestion/jobs/{job_id}")
    def cancel_ingestion(job_id: uuid.UUID):
        if not jobs.get(str(job_id)):
            raise HTTPException(404, "Ingestion job not found.")
        if not jobs.cancel(str(job_id)):
            raise HTTPException(409, "This job has already finished.")
        return {"status": "cancellation_requested"}

    @app.post("/api/ingestion/jobs/{job_id}/retry")
    def retry_ingestion(job_id: uuid.UUID):
        try:
            return jobs.retry(str(job_id))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/documents/{document_id}")
    def delete(document_id: str):
        existing = store.document(document_id)
        if existing:
            for job in jobs.list():
                if job["filename"] == existing["filename"]:
                    jobs.cancel(job["id"])
        if not store.delete(document_id):
            raise HTTPException(404, "Document not found.")
        return {"status": "deleted"}

    def run_search(query):
        if not query.question.strip():
            raise HTTPException(422, "Question must contain text.")
        try:
            return store.search(query.question, query.mode, query.k, query.document_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/search")
    async def search(query: Query, request: Request):
        start = time.perf_counter()
        async with work_slot():
            hits = await asyncio.to_thread(run_search, query)
        return {
            "hits": hits,
            "mode": query.mode,
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "request_id": request.state.request_id,
        }

    @app.post("/api/ask")
    async def ask(query: Query, request: Request):
        start = time.perf_counter()
        operation_id = str(query.operation_id or uuid.uuid4())
        if operation_id in active_asks:
            raise HTTPException(409, "This question is already running.")
        snapshot = providers.snapshot()

        async def execute():
            async with work_slot():
                hits = await asyncio.to_thread(run_search, query)
                try:
                    result = await asyncio.wait_for(
                        answer_async(
                            query.question,
                            hits,
                            replace(snapshot, llm_url="", llm_model="")
                            if query.answer_style == "evidence"
                            else snapshot,
                        ),
                        timeout=snapshot.llm_timeout,
                    )
                except TimeoutError:
                    result = {
                        "status": "generation_unavailable",
                        "error_code": "provider_timeout",
                        "message": "The model timed out. Source passages remain available. Check Model setup.",
                        "claims": [],
                        "usage": {},
                    }
                if query.answer_style == "evidence" and result["status"] == "evidence_only":
                    result["message"] = "Source excerpts selected. No generation model was called."
                return hits, result

        task = asyncio.create_task(execute())
        active_asks[operation_id] = task
        try:
            hits, result = await task
        except asyncio.CancelledError:
            return JSONResponse(
                {
                    "status": "cancelled",
                    "message": "Request cancelled. The model server may finish work already dispatched.",
                },
                status_code=499,
            )
        finally:
            active_asks.pop(operation_id, None)
        with state_lock:
            counters[result["status"]] += 1
        return {
            **result,
            "hits": hits,
            "mode": query.mode,
            "operation_id": operation_id,
            "provider": {"kind": snapshot.llm_kind, "model": snapshot.llm_model},
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "request_id": request.state.request_id,
        }

    @app.delete("/api/requests/{operation_id}")
    async def cancel_request(operation_id: uuid.UUID):
        task = active_asks.get(str(operation_id))
        if not task:
            raise HTTPException(404, "The request has already finished or is not running.")
        task.cancel()
        return {"status": "cancellation_requested"}

    @app.post("/api/feedback", status_code=201)
    def feedback(value: Feedback):
        store.save_feedback(str(value.request_id), value.helpful, value.note)
        return {"status": "saved"}

    @app.get("/api/readiness")
    def readiness():
        with store.connect() as db:
            integrity = db.execute("PRAGMA quick_check").fetchone()[0]
            chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            indexed = db.execute("SELECT COUNT(*) FROM search").fetchone()[0]
            missing = db.execute(
                "SELECT COUNT(*) FROM chunks c LEFT JOIN search s ON s.chunk_id=c.id WHERE s.chunk_id IS NULL"
            ).fetchone()[0]
            states = dict(db.execute("SELECT state,COUNT(*) FROM ingestion_jobs GROUP BY state"))
        healthy = integrity == "ok" and chunks == indexed and not missing
        return JSONResponse(
            {
                "status": "ready" if healthy else "degraded",
                "database": integrity,
                "chunks": chunks,
                "indexed": indexed,
                "missing_index_entries": missing,
                "ingestion_jobs": states,
                "model": "tested" if providers.public()["last_check"] else "untested",
                "answer_quality": "not_certified",
            },
            status_code=200 if healthy else 503,
        )

    @app.get("/api/metrics")
    def metrics():
        with state_lock:
            ordered = sorted(durations)
            return {
                "counts": dict(counters),
                "recent_requests": len(ordered),
                "p95_ms": round(ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)], 2)
                if ordered
                else 0,
            }

    @app.get("/api/openapi.json")
    def schema():
        return app.openapi()

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="web")
    return app
