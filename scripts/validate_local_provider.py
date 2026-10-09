"""Opt-in live smoke test. Uses public repository documentation, never private uploads.

This is an integration test, not an independent answer-quality benchmark.
"""

import argparse
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from docatlas.api import create_app
from docatlas.config import Settings
from docatlas.providers import ProviderInput, ProviderManager

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--model", required=True)
parser.add_argument("--url", default="http://127.0.0.1:1234/v1")
parser.add_argument("--context", type=int, default=4096)
parser.add_argument("--max-output", type=int, default=1024)
parser.add_argument(
    "--configure", action="store_true", help="Save this local provider in the workspace"
)
parser.add_argument("--output", default="data/live-provider-smoke.json")
args = parser.parse_args()
provider = ProviderInput(
    base_url=args.url,
    model=args.model,
    context_tokens=args.context,
    max_output_tokens=args.max_output,
    timeout_seconds=120,
)
report = {
    "type": "live integration smoke test",
    "independent_quality_benchmark": False,
    "semantic_correctness_checked": False,
    "review_required": "Read every claim against its quote; answered means citation format passed, not semantic correctness.",
    "model": args.model,
    "cases": [],
}
with tempfile.TemporaryDirectory() as directory:
    settings = replace(Settings.from_env(), data_dir=Path(directory), api_key="")
    with TestClient(create_app(settings)) as client:
        client.put("/api/provider", json=provider.model_dump()).raise_for_status()
        discovered = client.post("/api/provider/models", json=provider.model_dump())
        report["models_status"] = discovered.status_code
        probe = client.post("/api/provider/check", json=provider.model_dump())
        report["connection_test"] = {"http_status": probe.status_code, **probe.json()}
        upload = client.post(
            "/api/documents", files={"file": ("docatlas-readme.md", Path("README.md").read_bytes())}
        )
        upload.raise_for_status()
        for question in [
            "Which retrieval methods does DocAtlas support?",
            "Does exact quote verification prove semantic entailment?",
            "Can DocAtlas run without a configured model?",
            "What is the repository owner's private home address?",
        ]:
            response = client.post("/api/ask", json={"question": question})
            data = response.json()
            report["cases"].append(
                {
                    "question": question,
                    "http_status": response.status_code,
                    **{
                        key: data.get(key)
                        for key in (
                            "status",
                            "message",
                            "error_code",
                            "claims",
                            "usage",
                            "latency_ms",
                        )
                    },
                }
            )
path = Path(args.output)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
if args.configure and probe.status_code == 200:
    ProviderManager(Settings.from_env()).save(provider)
print(json.dumps(report, indent=2))
if probe.status_code != 200 or not any(c["status"] == "answered" for c in report["cases"]):
    raise SystemExit(1)
