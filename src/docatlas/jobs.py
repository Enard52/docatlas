"""Persistent, single-worker ingestion with isolated parser/model processes."""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .ingest import SUPPORTED

TERMINAL = {"ready", "failed", "cancelled"}
ACTIVE = {"queued", "extracting", "chunking", "embedding", "committing"}


def now():
    return datetime.now(UTC).isoformat()


def resident_mb(pid):
    """Observe worker RSS on supported Linux/macOS deployments."""
    if sys.platform.startswith("linux"):
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
        except (OSError, ValueError):
            return 0
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/bin/ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=1
            )
            return int(result.stdout.strip() or "0") / 1024
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 0
    return 0


class JobQueue:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.spool = settings.data_dir / "uploads"
        self.spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stop_event = threading.Event()
        self.thread = None
        self.process = None
        self.lock = threading.Lock()
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ingestion_jobs (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, source TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}',
                embeddings TEXT NOT NULL, embedding_model TEXT NOT NULL
            )""")

    def start(self):
        # Only one API process may own this workspace. Acquire an OS lock before recovery.
        import fcntl

        self.lock_file = open(self.spool / ".worker.lock", "a+")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise RuntimeError(
                "Another DocAtlas ingestion worker owns this data directory."
            ) from None
        with self.store.connect() as db:
            db.execute(
                "UPDATE ingestion_jobs SET state='queued',updated_at=? WHERE attempts < 3 AND state IN ('extracting','chunking','embedding','committing')",
                (now(),),
            )
        with self.store.connect() as db:
            db.execute(
                "UPDATE ingestion_jobs SET state='failed',error_code='worker_failed',error='Interrupted repeatedly. Correct the cause and upload again.',updated_at=? WHERE attempts>=3 AND state IN ('extracting','chunking','embedding','committing')",
                (now(),),
            )
            expired = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM ingestion_jobs WHERE state='failed' AND datetime(updated_at) < datetime('now','-7 days')"
                )
            ]
            known = {r[0] for r in db.execute("SELECT id FROM ingestion_jobs")}
        for job_id in expired:
            self.source_path(job_id).unlink(missing_ok=True)
        for path in self.spool.glob("*.source"):
            if path.stem not in known:
                path.unlink(missing_ok=True)
        # Sources for completed jobs have no purpose; interrupted cleanup is safe to repeat.
        for job in self.list():
            if job["state"] in {"ready", "cancelled"} or (
                job["state"] == "failed" and job["error_code"] == "invalid_document"
            ):
                self.source_path(job["id"]).unlink(missing_ok=True)
        self.thread = threading.Thread(target=self._loop, daemon=True, name="docatlas-ingestion")
        self.thread.start()

    def close(self):
        self.stop_event.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
        if self.thread:
            self.thread.join(timeout=8)
        if hasattr(self, "lock_file"):
            self.lock_file.close()

    def source_path(self, job_id):
        return self.spool / (str(uuid.UUID(job_id)) + ".source")

    def submit(self, filename, data, source=""):
        filename = Path(filename.replace("\\", "/")).name[:200]
        if not filename or Path(filename).suffix.lower() not in SUPPORTED:
            raise ValueError("Supported files: Markdown, TXT, HTML, DOCX and text-based PDF.")
        if len(data) > self.settings.max_upload_mb * 1024 * 1024:
            raise ValueError("File exceeds the upload limit.")
        if source:
            import re

            if len(source) > 2000 or not re.match(r"^https?://[^\s]+$", source):
                raise ValueError("Source must be an HTTP(S) URL.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM ingestion_jobs WHERE state NOT IN ('ready','failed','cancelled')"
                ).fetchone()[0]
                >= 32
            ):
                raise ValueError("Ingestion queue is full. Wait for existing uploads to finish.")
            job_id = str(uuid.uuid4())
            path = self.source_path(job_id)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                db.execute(
                    "INSERT INTO ingestion_jobs (id,filename,source,state,created_at,updated_at,embeddings,embedding_model) VALUES (?,?,?,'queued',?,?,?,?)",
                    (
                        job_id,
                        filename,
                        source,
                        now(),
                        now(),
                        self.settings.embeddings,
                        self.settings.embedding_model,
                    ),
                )
            except Exception:
                path.unlink(missing_ok=True)
                raise
        return self.get(job_id)

    def get(self, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["result"] = json.loads(result["result"])
        return result

    def list(self):
        with self.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM ingestion_jobs ORDER BY created_at DESC LIMIT 100"
                )
            ]
        return [self.get(job_id) for job_id in ids]

    def cancel(self, job_id):
        with self.store.connect() as db:
            changed = db.execute(
                "UPDATE ingestion_jobs SET state='cancelled',updated_at=? WHERE id=? AND state NOT IN ('ready','failed','cancelled')",
                (now(), job_id),
            ).rowcount
        # The worker observes the state and terminates the isolated process. Commit
        # checks the state in the same write transaction as the document replacement.
        if changed:
            self.source_path(job_id).unlink(missing_ok=True)
        return bool(changed)

    def retry(self, job_id):
        job = self.get(job_id)
        if not job or job["state"] != "failed":
            raise ValueError("Only failed jobs can be retried.")
        if job["error_code"] == "invalid_document":
            raise ValueError(
                "Correct this document and upload it again; retry cannot repair its content."
            )
        if job["attempts"] >= 3:
            raise ValueError("Retry limit reached. Correct the cause and upload again.")
        if not self.source_path(job_id).exists():
            raise ValueError("Upload the source again; this job no longer retains its source.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM ingestion_jobs WHERE state NOT IN ('ready','failed','cancelled')"
                ).fetchone()[0]
                >= 32
            ):
                raise ValueError("Ingestion queue is full. Wait before retrying.")
            db.execute(
                "UPDATE ingestion_jobs SET state='queued',error='',error_code='',updated_at=? WHERE id=? AND state='failed'",
                (now(), job_id),
            )
        return self.get(job_id)

    def fail(self, job_id, code, message):
        with self.store.connect() as db:
            db.execute(
                "UPDATE ingestion_jobs SET state='failed',error_code=?,error=?,updated_at=? WHERE id=? AND state NOT IN ('ready','cancelled')",
                (code, message, now(), job_id),
            )

    def cleanup_expired_sources(self):
        with self.store.connect() as db:
            expired = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM ingestion_jobs WHERE state='failed' AND datetime(updated_at) < datetime('now','-7 days')"
                )
            ]
        for job_id in expired:
            self.source_path(job_id).unlink(missing_ok=True)

    def _loop(self):
        sweep_at = 0.0
        while not self.stop_event.is_set():
            if time.monotonic() > sweep_at:
                self.cleanup_expired_sources()
                sweep_at = time.monotonic() + 3600
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT id FROM ingestion_jobs WHERE state='queued' ORDER BY created_at LIMIT 1"
                ).fetchone()
                if row:
                    db.execute(
                        "UPDATE ingestion_jobs SET state='extracting',attempts=attempts+1,updated_at=? WHERE id=? AND state='queued'",
                        (now(), row[0]),
                    )
            if not row:
                self.stop_event.wait(0.2)
                continue
            job_id = row[0]
            try:
                self._run(job_id)
            except Exception:
                # Never store exception text containing document content/credentials.
                self.fail(
                    job_id,
                    "worker_failed",
                    "Ingestion worker failed. Check available disk and memory, then retry.",
                )
            job = self.get(job_id)
            if job and job["state"] in {"ready", "cancelled"}:
                self.source_path(job_id).unlink(missing_ok=True)
            elif job and job["state"] == "failed" and job["error_code"] == "invalid_document":
                self.source_path(job_id).unlink(missing_ok=True)

    def _run(self, job_id):
        env = os.environ.copy()
        # Workers never need generation or workspace credentials.
        for key in ("DOCATLAS_LLM_KEY", "DOCATLAS_API_KEY"):
            env.pop(key, None)
        with self.lock:
            if self.stop_event.is_set():
                return
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "docatlas.job_worker",
                    str(self.settings.data_dir.resolve()),
                    job_id,
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.process = process
        deadline = time.monotonic() + self.settings.ingestion_timeout
        memory_check = 0.0
        over_memory = False
        while process.poll() is None:
            if time.monotonic() >= memory_check:
                over_memory = resident_mb(process.pid) > self.settings.worker_memory_mb
                memory_check = time.monotonic() + 0.5
            job = self.get(job_id)
            if (
                self.stop_event.is_set()
                or job["state"] == "cancelled"
                or time.monotonic() > deadline
                or over_memory
            ):
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                if not self.stop_event.is_set() and job["state"] != "cancelled":
                    self.fail(
                        job_id,
                        "memory_limit" if over_memory else "ingestion_timeout",
                        "Ingestion exceeded its memory limit. Try a smaller document or raise DOCATLAS_WORKER_MEMORY_MB."
                        if over_memory
                        else "Ingestion exceeded its time limit. Try a smaller document or raise the worker deadline.",
                    )
                break
            self.stop_event.wait(0.1)
        with self.lock:
            self.process = None
        if not self.stop_event.is_set() and self.get(job_id)["state"] not in TERMINAL:
            self.fail(
                job_id,
                "worker_failed",
                "The isolated worker stopped unexpectedly. Retry after checking memory and disk.",
            )
