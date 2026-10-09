import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from docatlas.api import create_app
from docatlas.config import Settings
from docatlas.ingest import extract
from docatlas.jobs import JobQueue
from docatlas.store import Store


def wait_job(queue, job_id, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = queue.get(job_id)
        if job["state"] in {"ready", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError("Worker did not finish")


def test_durable_worker_indexes_and_cleans_source(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/api/ingestion/jobs",
            files={"file": ("notes.md", b"# Notes\nThe release uses a PostgreSQL database.")},
        )
        assert response.status_code == 202
        job = wait_job(app.state.jobs, response.json()["id"])
        assert job["state"] == "ready", job
        assert app.state.store.search("PostgreSQL", "bm25")
        assert client.get(f"/api/ingestion/jobs/{job['id']}").json()["result"]["id"]
    assert not app.state.jobs.source_path(job["id"]).exists()


def test_restart_recovers_interrupted_job_and_failed_replacement_preserves_old(tmp_path):
    settings = Settings(data_dir=tmp_path)
    store = Store(tmp_path)
    original = store.ingest("guide.md", b"# Guide\nKeep the original searchable database text.")
    queue = JobQueue(store, settings)
    job = queue.submit("guide.md", b"\xff")
    with store.connect() as db:
        db.execute(
            "UPDATE ingestion_jobs SET state='embedding',attempts=1 WHERE id=?", (job["id"],)
        )
    recovered = JobQueue(store, settings)
    recovered.start()
    try:
        outcome = wait_job(recovered, job["id"])
        assert outcome["state"] == "failed"
        assert outcome["error_code"] == "invalid_document"
        assert "original" in store.document(original["id"])["passages"][0]["text"]
        with pytest.raises(ValueError, match="Correct"):
            recovered.retry(job["id"])
    finally:
        recovered.close()


def test_cancel_before_commit_keeps_document_and_job_consistent(tmp_path):
    store = Store(tmp_path)
    queue = JobQueue(store, Settings(data_dir=tmp_path))
    original = store.ingest("guide.md", b"The original database text remains searchable.")
    job = queue.submit("guide.md", b"A replacement removes the database text.")

    def cancel_at_commit(stage):
        if stage == "committing":
            queue.cancel(job["id"])

    with pytest.raises(ValueError, match="cancelled"):
        store.ingest(job["filename"], b"Replacement", progress=cancel_at_commit, job_id=job["id"])
    assert queue.get(job["id"])["state"] == "cancelled"
    assert "original" in store.document(original["id"])["passages"][0]["text"]
    assert not queue.source_path(job["id"]).exists()


def test_worker_lock_and_bounded_queue(tmp_path):
    settings = Settings(data_dir=tmp_path)
    first = JobQueue(Store(tmp_path), settings)
    first.start()
    second = JobQueue(Store(tmp_path), settings)
    try:
        with pytest.raises(RuntimeError, match="Another"):
            second.start()
    finally:
        first.close()
    queue = JobQueue(Store(tmp_path), settings)
    for i in range(32):
        queue.submit(f"{i}.txt", b"Notes")
    with pytest.raises(ValueError, match="full"):
        queue.submit("overflow.txt", b"Notes")


def test_backup_restore_preserves_search_results(tmp_path):
    store = Store(tmp_path / "original")
    store.ingest("guide.md", b"PostgreSQL database deployment notes.")
    target = tmp_path / "restored"
    target.mkdir()
    store.backup(target / "docatlas.sqlite3")
    restored = Store(target)
    assert restored.fingerprint() == store.fingerprint()
    assert restored.search("PostgreSQL", "bm25") == store.search("PostgreSQL", "bm25")


def docx(xml):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


def test_docx_paragraphs_and_table_cells():
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Release notes</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Latency</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>20ms</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
    text = extract("report.docx", docx(xml))[0][1]
    assert "Release notes" in text and "Latency | 20ms" in text
    assert "verify merged cells" in text
    with pytest.raises(ValueError):
        extract("report.docx", docx('<!DOCTYPE x [<!ENTITY a "injected">]><x>&a;</x>'))
    with pytest.raises(ValueError):
        extract("report.docx", b"bad")


def test_job_authorization(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, api_key="secret"))) as client:
        assert client.get("/api/ingestion/jobs").status_code == 401
        assert (
            client.post("/api/ingestion/jobs", files={"file": ("notes.txt", b"Notes")}).status_code
            == 401
        )
        assert client.get("/api/ingestion/jobs", headers={"X-API-Key": "secret"}).status_code == 200


@pytest.mark.parametrize(
    "failure,expected", [("memory", "memory_limit"), ("timeout", "ingestion_timeout")]
)
def test_isolated_worker_resource_limits_preserve_existing_document(
    tmp_path, monkeypatch, failure, expected
):
    import subprocess
    import sys
    from dataclasses import replace

    settings = replace(
        Settings(data_dir=tmp_path), ingestion_timeout=0 if failure == "timeout" else 180
    )
    store = Store(tmp_path)
    store.ingest("notes.txt", b"Original database notes.")
    queue = JobQueue(store, settings)
    job = queue.submit("notes.txt", b"Replacement")
    real_popen = subprocess.Popen

    def slow_worker(*args, **kwargs):
        return real_popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)

    monkeypatch.setattr("docatlas.jobs.subprocess.Popen", slow_worker)
    monkeypatch.setattr("docatlas.jobs.resident_mb", lambda pid: 2000 if failure == "memory" else 0)
    queue._run(job["id"])
    assert queue.get(job["id"])["error_code"] == expected
    assert store.search("Original", "bm25")
    assert queue.source_path(job["id"]).exists()


def test_pdf_missing_page_text_is_visible():
    from pypdf import PdfWriter

    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    warnings = []
    with pytest.raises(ValueError, match="OCR"):
        extract("scan.pdf", output.getvalue(), warnings=warnings)
    assert any("Page 1" in warning for warning in warnings)


def test_readiness_detects_missing_index_entries(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    app.state.store.ingest("notes.txt", b"Database deployment notes.")
    with TestClient(app) as client:
        assert client.get("/api/readiness").json()["answer_quality"] == "not_certified"
        with app.state.store.connect() as db:
            db.execute("DELETE FROM search")
        assert client.get("/api/readiness").status_code == 503


def test_excerpts_style_never_calls_configured_model(tmp_path, monkeypatch):
    async def unexpected_generation(*args, **kwargs):
        raise AssertionError("Evidence style called a model")

    monkeypatch.setattr("docatlas.answer.generate_async", unexpected_generation)
    settings = Settings(data_dir=tmp_path, llm_url="http://127.0.0.1:1234/v1", llm_model="chat")
    app = create_app(settings)
    app.state.store.ingest("notes.txt", b"The PostgreSQL database uses connection pooling.")
    with TestClient(app) as client:
        result = client.post(
            "/api/ask", json={"question": "PostgreSQL database", "answer_style": "evidence"}
        ).json()
        assert result["status"] == "evidence_only"
        assert result["hits"]
        assert "No generation model was called" in result["message"]


def test_legacy_schema_is_backed_up_before_migration(tmp_path):
    import sqlite3

    path = tmp_path / "docatlas.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE documents (id TEXT PRIMARY KEY, filename TEXT NOT NULL UNIQUE, digest TEXT NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL, byte_size INTEGER NOT NULL, embedding_model TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO documents VALUES ('legacy','notes.txt','hash','','2026-01-01',5,'none')"
        )
    upgraded = Store(tmp_path)
    assert upgraded.list_documents()[0]["extraction_profile"] == "legacy"
    backup_path = tmp_path / "backups" / "pre-mixed-v2.sqlite3"
    with sqlite3.connect(backup_path) as db:
        assert db.execute("SELECT filename FROM documents").fetchone()[0] == "notes.txt"
        assert "extraction_profile" not in {
            r[1] for r in db.execute("PRAGMA table_info(documents)")
        }


def test_profile_change_reingests_unchanged_bytes(tmp_path):
    store = Store(tmp_path)
    content = b"Database deployment notes."
    first = store.ingest("notes.txt", content)
    with store.connect() as db:
        db.execute("UPDATE documents SET extraction_profile='legacy'")
    assert store.ingest("notes.txt", content)["status"] == "updated"
    assert store.document(first["id"])["extraction_profile"] == "mixed-v2"


def test_newer_schema_is_not_silently_downgraded(tmp_path):
    store = Store(tmp_path)
    with store.connect() as db:
        db.execute("INSERT INTO schema_migrations VALUES (99,'future')")
    with pytest.raises(ValueError, match="newer schema"):
        Store(tmp_path)
