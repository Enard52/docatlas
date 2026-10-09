"""SQLite document store and FTS5 index, with exact dense search for small corpora."""

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .ingest import EXTRACTION_PROFILE, chunk_document, extract
from .retrieval import reciprocal_rank_fusion


class Store:
    def __init__(self, directory: Path, encoder=None):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "docatlas.sqlite3"
        if self.path.exists():
            previous = sqlite3.connect(self.path)
            try:
                columns = {r[1] for r in previous.execute("PRAGMA table_info(documents)")}
                if columns and "extraction_profile" not in columns:
                    backup_dir = directory / "backups"
                    backup_dir.mkdir(mode=0o700, exist_ok=True)
                    backup_path = backup_dir / "pre-mixed-v2.sqlite3"
                    if not backup_path.exists():
                        fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        os.close(fd)
                        target = sqlite3.connect(backup_path)
                        try:
                            previous.backup(target)
                        finally:
                            target.close()
            finally:
                previous.close()
        self.encoder = encoder
        self._writer = threading.Lock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL UNIQUE,
                    digest TEXT NOT NULL, source TEXT NOT NULL,
                    updated_at TEXT NOT NULL, byte_size INTEGER NOT NULL,
                    embedding_model TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id)
                    ON DELETE CASCADE, text TEXT NOT NULL, section TEXT NOT NULL,
                    page INTEGER, ordinal INTEGER NOT NULL, vector BLOB
                );
                CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(document_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
                    chunk_id UNINDEXED, text, tokenize='porter unicode61'
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, helpful INTEGER NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(documents)")}
            for name, definition in [
                ("extraction_profile", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("warnings", "TEXT NOT NULL DEFAULT '[]'"),
            ]:
                if name not in columns:
                    db.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (1,?)",
                (datetime.now(UTC).isoformat(),),
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, filename: str, content: bytes, source: str = "", progress=None, job_id=None):
        progress = progress or (lambda stage: None)
        filename = Path(filename.replace("\\", "/")).name[:200]
        if not filename:
            raise ValueError("A filename is required.")
        if source and not re.match(r"^https?://[^\s]+$", source):
            raise ValueError("Source must be an HTTP(S) URL.")
        document_id = hashlib.sha256(filename.encode()).hexdigest()[:24]
        digest = hashlib.sha256(content).hexdigest()
        model = self.encoder.name if self.encoder else "none"
        # Serialize writers, but keep readers available while extracting/embedding.
        with self._writer:
            with self.connect() as db:
                existing = db.execute(
                    "SELECT * FROM documents WHERE id=?", (document_id,)
                ).fetchone()
            if (
                existing
                and existing["digest"] == digest
                and existing["embedding_model"] == model
                and existing["source"] == source
                and existing["extraction_profile"] == EXTRACTION_PROFILE
            ):
                result = {"id": document_id, "status": "unchanged"}
                if job_id:
                    with self.connect() as db:
                        self._finish_job(db, job_id, result)
                return result
            warnings = []
            progress("extracting")
            pages = extract(filename, content, warnings=warnings)
            progress("chunking")
            chunks = chunk_document(document_id, pages)
            progress("embedding")
            vectors = self.encoder.documents([c.text for c in chunks]) if self.encoder else None
            progress("committing")
            result = {
                "id": document_id,
                "status": "updated" if existing else "created",
                "chunks": len(chunks),
                "warnings": warnings,
            }
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if job_id:
                    self._finish_job(db, job_id, result)
                # One transaction removes old chunks, stores new chunks and updates FTS.
                db.execute(
                    "DELETE FROM search WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)",
                    (document_id,),
                )
                db.execute("DELETE FROM documents WHERE id=?", (document_id,))
                db.execute(
                    "INSERT INTO documents (id,filename,digest,source,updated_at,byte_size,embedding_model,extraction_profile,warnings) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        document_id,
                        filename,
                        digest,
                        source,
                        datetime.now(UTC).isoformat(),
                        len(content),
                        model,
                        EXTRACTION_PROFILE,
                        json.dumps(warnings),
                    ),
                )
                for i, chunk in enumerate(chunks):
                    vector = (
                        vectors[i].astype(np.float32).tobytes() if vectors is not None else None
                    )
                    db.execute(
                        "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                        (
                            chunk.id,
                            document_id,
                            chunk.text,
                            chunk.section,
                            chunk.page,
                            chunk.ordinal,
                            vector,
                        ),
                    )
                    db.execute("INSERT INTO search VALUES (?,?)", (chunk.id, chunk.text))
            return result

    def _finish_job(self, db, job_id, result):
        changed = db.execute(
            "UPDATE ingestion_jobs SET state='ready',result=?,updated_at=? WHERE id=? AND state NOT IN ('cancelled','failed','ready')",
            (json.dumps(result), datetime.now(UTC).isoformat(), job_id),
        ).rowcount
        if not changed:
            raise ValueError("Ingestion was cancelled before commit.")

    def backup(self, destination):
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()

    def list_documents(self):
        with self.connect() as db:
            return [
                {**dict(row), "warnings": json.loads(row["warnings"])}
                for row in db.execute("""
                SELECT d.*, COUNT(c.id) AS chunks FROM documents d
                LEFT JOIN chunks c ON c.document_id=d.id GROUP BY d.id ORDER BY d.filename
            """)
            ]

    def document(self, document_id):
        with self.connect() as db:
            doc = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if not doc:
                return None
            return {
                **dict(doc),
                "warnings": json.loads(doc["warnings"]),
                "passages": [
                    dict(c)
                    for c in db.execute(
                        "SELECT id,text,section,page,ordinal FROM chunks WHERE document_id=? ORDER BY ordinal",
                        (document_id,),
                    )
                ],
            }

    def delete(self, document_id):
        with self._writer, self.connect() as db:
            db.execute(
                "DELETE FROM search WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)",
                (document_id,),
            )
            return db.execute("DELETE FROM documents WHERE id=?", (document_id,)).rowcount > 0

    def search(self, question, mode="hybrid", k=6, document_id=None):
        if mode not in {"bm25", "dense", "hybrid"}:
            raise ValueError("Unknown retrieval mode.")
        if mode != "bm25" and not self.encoder:
            raise ValueError(
                "Neural retrieval is disabled. Enable fastembed and re-ingest documents."
            )
        tokens = list(dict.fromkeys(re.findall(r"\w+", question.lower())))[:40]
        if not tokens:
            return []
        query = " OR ".join('"' + token + '"' for token in tokens)
        limit = max(k * 4, 30)
        if mode == "bm25":
            # Fetch only ranked matches, never the complete library or vector blobs.
            with self.connect() as db:
                rows = db.execute(
                    "SELECT c.id,c.document_id,c.text,c.section,c.page,c.ordinal,d.filename,d.source,bm25(search) AS rank FROM search JOIN chunks c ON c.id=search.chunk_id JOIN documents d ON d.id=c.document_id WHERE search MATCH ?"
                    + (" AND c.document_id=?" if document_id else "")
                    + " ORDER BY rank,c.id LIMIT ?",
                    (query, *((document_id,) if document_id else ()), k),
                ).fetchall()
            results = []
            for index, row in enumerate(rows, 1):
                value = dict(row)
                rank = value.pop("rank")
                results.append(
                    {
                        **value,
                        "score": round(1 / (60 + index), 6),
                        "bm25_score": -rank,
                        "dense_score": None,
                    }
                )
            return results
        with self.connect() as db:
            # Keep all reads on one snapshot across simultaneous ingestion/deletion.
            db.execute("BEGIN")
            filters = " WHERE c.document_id=?" if document_id else ""
            params = (document_id,) if document_id else ()
            rows = db.execute(
                """
                SELECT c.*,d.filename,d.source,d.embedding_model FROM chunks c
                JOIN documents d ON c.document_id=d.id
            """
                + filters,
                params,
            ).fetchall()
            records = {r["id"]: dict(r) for r in rows}
            if not records:
                return []
            bm_scores = {}
            if mode in {"bm25", "hybrid"}:
                # Apply corpus filter BEFORE top-k, not after.
                bm_rows = db.execute(
                    """
                    SELECT search.chunk_id,bm25(search) AS score FROM search
                    JOIN chunks c ON c.id=search.chunk_id WHERE search MATCH ?
                """
                    + (" AND c.document_id=?" if document_id else "")
                    + " ORDER BY score LIMIT ?",
                    (query, *params, limit),
                ).fetchall()
                bm_scores = {r["chunk_id"]: -r["score"] for r in bm_rows}
            dense_scores = {}
            if mode in {"dense", "hybrid"}:
                if any(
                    r["embedding_model"] != self.encoder.name or r["vector"] is None for r in rows
                ):
                    raise ValueError(
                        "Embedding index is stale. Re-ingest all selected documents with the current model."
                    )
                q = self.encoder.query(question)
                vectors = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
                if vectors.shape[1] != q.shape[0]:
                    raise ValueError("Embedding dimensions changed. Re-ingest the corpus.")
                similarities = vectors @ q
                order = np.argsort(-similarities, kind="stable")[:limit]
                dense_scores = {rows[int(i)]["id"]: float(similarities[i]) for i in order}
        rankings = [list(bm_scores)] if mode == "bm25" else [list(dense_scores)]
        if mode == "hybrid":
            rankings = [list(bm_scores), list(dense_scores)]
        result = []
        for key, score in reciprocal_rank_fusion(rankings)[:k]:
            row = records[key]
            row.pop("vector")
            row.pop("embedding_model")
            result.append(
                {
                    **row,
                    "score": round(score, 6),
                    "bm25_score": bm_scores.get(key, 0),
                    "dense_score": dense_scores.get(key),
                }
            )
        return result

    def save_feedback(self, request_id, helpful, note):
        with self.connect() as db:
            db.execute(
                "INSERT INTO feedback(request_id,helpful,note,created_at) VALUES(?,?,?,?)",
                (
                    request_id,
                    int(helpful),
                    note,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def fingerprint(self):
        docs = self.list_documents()
        data = [(d["id"], d["digest"], d["embedding_model"], d["extraction_profile"]) for d in docs]
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:16]
