"""Private ingestion worker; launched by JobQueue, never a network service."""

import sys
from datetime import UTC, datetime
from pathlib import Path

from .retrieval import LocalEncoder
from .store import Store


def main():
    directory, job_id = Path(sys.argv[1]), sys.argv[2]
    store = Store(directory)
    with store.connect() as db:
        job = dict(db.execute("SELECT * FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone())

    stage_name = "embedding" if job["embeddings"] == "fastembed" else "extracting"

    def progress(stage):
        nonlocal stage_name
        stage_name = stage
        with store.connect() as db:
            if not db.execute(
                "UPDATE ingestion_jobs SET state=?,updated_at=? WHERE id=? AND state NOT IN ('cancelled','ready')",
                (stage, datetime.now(UTC).isoformat(), job_id),
            ).rowcount:
                raise ValueError("Ingestion was cancelled.")

    try:
        if job["embeddings"] == "fastembed":
            store.encoder = LocalEncoder(job["embedding_model"], directory / "models")
        source = directory / "uploads" / (job_id + ".source")
        store.ingest(
            job["filename"], source.read_bytes(), job["source"], progress=progress, job_id=job_id
        )
    except Exception as exc:
        invalid = isinstance(exc, ValueError) and stage_name in {"extracting", "chunking"}
        message = (
            str(exc)[:500]
            if invalid
            else "Ingestion failed. Check available memory, model access and disk, then retry."
        )
        with store.connect() as db:
            db.execute(
                "UPDATE ingestion_jobs SET state='failed',error_code=?,error=?,updated_at=? WHERE id=? AND state NOT IN ('cancelled','ready')",
                (
                    "invalid_document" if invalid else "worker_failed",
                    message,
                    datetime.now(UTC).isoformat(),
                    job_id,
                ),
            )


if __name__ == "__main__":
    main()
