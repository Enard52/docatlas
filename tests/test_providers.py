import asyncio
import json
import stat
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from docatlas.answer import ProviderError, generate_async, request_body
from docatlas.api import create_app
from docatlas.config import Settings
from docatlas.providers import ProviderInput, ProviderManager


def test_keys_persist_privately_but_never_cross_endpoints(tmp_path):
    manager = ProviderManager(Settings(data_dir=tmp_path))
    manager.save(
        ProviderInput(
            kind="api", base_url="https://first.test/v1", model="a", api_key="private-key"
        )
    )
    assert "private-key" not in json.dumps(manager.public())
    assert stat.S_IMODE(manager.path.stat().st_mode) == 0o600
    restored = ProviderManager(Settings(data_dir=tmp_path))
    assert restored.snapshot().llm_key == "private-key"
    restored.save(ProviderInput(kind="api", base_url="https://first.test/v1", model="b"))
    assert restored.snapshot().llm_key == "private-key"
    restored.save(ProviderInput(kind="api", base_url="https://other.test/v1", model="c"))
    assert restored.snapshot().llm_key == ""
    restored.save(
        ProviderInput(kind="api", base_url="https://other.test/v1", api_key="new", clear_key=True)
    )
    assert restored.snapshot().llm_key == ""


@pytest.mark.parametrize(
    "values",
    [
        {"base_url": "http://user:secret@127.0.0.1:1234/v1"},
        {"base_url": "http://127.0.0.1:1234/v1?key=secret"},
        {"base_url": "http://remote.test/v1"},
        {"kind": "api", "base_url": "http://remote.test/v1"},
        {"base_url": "http://127.0.0.1:1234/v1/chat/completions"},
        {"context_tokens": 2048, "max_output_tokens": 1400},
    ],
)
def test_invalid_provider_contracts_rejected(values):
    with pytest.raises(ValidationError):
        ProviderInput(**values)


def test_settings_api_masks_secrets_and_validation_input(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, api_key="workspace"))) as client:
        assert client.get("/api/provider").status_code == 401
        headers = {"X-API-Key": "workspace"}
        payload = {
            "kind": "api",
            "base_url": "https://provider.test/v1",
            "model": "chat",
            "api_key": "private-key",
        }
        response = client.put("/api/provider", headers=headers, json=payload)
        assert response.status_code == 200 and "private-key" not in response.text
        assert "private-key" not in client.get("/api/config", headers=headers).text
        payload["timeout_seconds"] = 0
        error = client.put("/api/provider", headers=headers, json=payload)
        assert error.status_code == 422 and "private-key" not in error.text
        assert (
            client.put(
                "/api/provider", headers={**headers, "Origin": "https://evil.test"}, json=payload
            ).status_code
            == 403
        )


def test_remote_client_cannot_change_model_settings(tmp_path):
    async def run():
        app = create_app(Settings(data_dir=tmp_path))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 123)),
            base_url="http://test",
        ) as client:
            assert (await client.get("/api/provider")).status_code == 403
            assert (await client.put("/api/provider", json={})).status_code == 403

    asyncio.run(run())


def test_context_budget_preserves_complete_passages_and_bounds_request():
    hits = [
        {"id": "huge", "text": "x" * 20000},
        {"id": "fits", "text": "Use the database URL environment variable."},
    ]
    settings = Settings(llm_context_tokens=4096, llm_max_tokens=512)
    body, supplied = request_body("database URL", hits, settings)
    assert [h["id"] for h in supplied] == ["fits"]
    assert body["response_format"]["type"] == "json_schema"
    with pytest.raises(ProviderError, match="No complete source passage"):
        request_body("question", hits[:1], settings)


def test_async_generation_handles_truncation_and_bad_citations():
    async def run():
        settings = Settings(llm_url="http://127.0.0.1:1234/v1", llm_model="chat")
        hit = {
            "id": "evidence",
            "text": "The deployment uses the database URL environment variable.",
        }
        for payload, code in [
            (
                {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
                "output_truncated",
            ),
            (
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "claims": [
                                            {
                                                "text": "Answer",
                                                "chunk_id": "invented",
                                                "quote": hit["text"],
                                            }
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                },
                "invalid_citation",
            ),
        ]:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _, payload=payload: httpx.Response(200, json=payload)
                )
            ) as client:
                with pytest.raises(ProviderError) as error:
                    await generate_async("database URL", [hit], settings, client)
                assert error.value.code == code

    asyncio.run(run())


def test_cancel_running_answer_releases_slot_and_rejects_duplicate(tmp_path, monkeypatch):
    async def run():
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_answer(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr("docatlas.api.answer_async", slow_answer)
        app = create_app(Settings(data_dir=tmp_path))
        app.state.store.ingest(
            "guide.md", b"# Database\nUse the database URL environment variable."
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)),
            base_url="http://test",
        ) as client:
            op = str(uuid.uuid4())
            query = {"question": "database URL", "operation_id": op}
            pending = asyncio.create_task(client.post("/api/ask", json=query))
            await asyncio.wait_for(entered.wait(), 2)
            assert (await client.post("/api/ask", json=query)).status_code == 409
            assert (await client.delete(f"/api/requests/{op}")).status_code == 200
            assert (await pending).status_code == 499
            assert cancelled.is_set()
            assert (await client.delete(f"/api/requests/{op}")).status_code == 404

    asyncio.run(run())


def test_probe_checks_answer_and_does_not_certify_quality(tmp_path, monkeypatch):
    async def probe(*args):
        return [{"text": "atlas-ready", "quote": "atlas-ready", "chunk_id": "connection-probe"}], {}

    monkeypatch.setattr("docatlas.api.generate_async", probe)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        value = ProviderInput(model="chat").model_dump()
        client.put("/api/provider", json=value).raise_for_status()
        result = client.post("/api/provider/check", json=value)
        assert result.status_code == 200
        assert "quality still requires evaluation" in result.json()["message"]
        assert client.get("/api/provider").json()["last_check"]["status"] == "ready"
        value["max_output_tokens"] = 1200
        client.put("/api/provider", json=value).raise_for_status()
        assert client.get("/api/provider").json()["last_check"] is None


def test_probe_rejects_wrong_answer(tmp_path, monkeypatch):
    async def wrong_probe(*args):
        return [{"text": "incorrect", "quote": "incorrect", "chunk_id": "connection-probe"}], {}

    monkeypatch.setattr("docatlas.api.generate_async", wrong_probe)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        result = client.post("/api/provider/check", json=ProviderInput(model="chat").model_dump())
        assert result.status_code == 502
        assert result.json()["code"] == "probe_failed"
        assert client.get("/api/provider").json()["last_check"] is None


def test_quote_check_does_not_misrepresent_semantic_verification():
    from docatlas.answer import success, validate_claims

    hit = {"id": "a", "text": "Every generated claim must reference a retrieved passage."}
    # This intentionally unsupported claim reproduces the live test's failure class.
    payload = {
        "claims": [
            {"text": "The app works without a model.", "quote": hit["text"], "chunk_id": "a"}
        ]
    }
    result = success(validate_claims(payload, [hit]), {})
    assert result["verification"]["semantic_support"] == "not_verified"
    assert "Draft answer" in result["message"]
