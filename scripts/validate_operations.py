"""Synthetic local load and backup/restore checks, not an answer-quality benchmark."""

import argparse
import json
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from docatlas.store import Store

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--documents", type=int, default=500)
parser.add_argument("--queries", type=int, default=200)
parser.add_argument("--output", type=Path, default=Path("data/operations-validation.json"))
args = parser.parse_args()
if not 10 <= args.documents <= 10000 or not 20 <= args.queries <= 2000:
    parser.error("Use 10–10000 documents and 20–2000 queries.")
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    store = Store(root / "source")
    for index in range(args.documents):
        store.ingest(
            f"note-{index:05d}.md",
            f"# Deployment note {index}\nThe PostgreSQL database uses connection pooling. Record marker{index} in the release notes.".encode(),
        )

    def measure(index):
        start = time.perf_counter()
        hits = store.search(f"marker{index % args.documents}", "bm25")
        if not hits or hits[0]["filename"] != f"note-{index % args.documents:05d}.md":
            raise AssertionError("Search did not return the expected synthetic document")
        return (time.perf_counter() - start) * 1000

    for index in range(10):
        measure(index)
    with ThreadPoolExecutor(max_workers=4) as executor:
        durations = sorted(executor.map(measure, range(args.queries)))
    restored_dir = root / "restored"
    restored_dir.mkdir()
    store.backup(restored_dir / "docatlas.sqlite3")
    restored = Store(restored_dir)
    if restored.fingerprint() != store.fingerprint():
        raise AssertionError("Restored document fingerprint differs")
    for index in (0, args.documents // 2, args.documents - 1):
        if restored.search(f"marker{index}", "bm25") != store.search(f"marker{index}", "bm25"):
            raise AssertionError("Restored search result differs")
    report = {
        "type": "synthetic local operations validation",
        "human_reviewed_quality_benchmark": False,
        "documents": args.documents,
        "queries": args.queries,
        "concurrency": 4,
        "bm25_p50_ms": round(statistics.median(durations), 3),
        "bm25_p95_ms": round(durations[int(len(durations) * 0.95) - 1], 3),
        "backup_restore": "passed: fingerprint and three query results match",
        "limitations": "Short synthetic Markdown only; excludes extraction, embeddings, model generation, real corpus representativeness and production SLA.",
    }
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
