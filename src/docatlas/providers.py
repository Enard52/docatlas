"""Workspace-owner provider configuration. Secrets never enter API responses or logs."""

import json
import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, model_validator

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


class ProviderInput(BaseModel):
    kind: Literal["lmstudio", "api", "disabled"] = "lmstudio"
    base_url: str = Field(default="http://127.0.0.1:1234/v1", max_length=2000)
    model: str = Field(default="", max_length=300)
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    clear_key: bool = False
    response_format: Literal["json_schema", "json_object"] = "json_schema"
    timeout_seconds: int = Field(default=120, ge=10, le=300)
    context_tokens: int = Field(default=8192, ge=2048, le=131072)
    max_output_tokens: int = Field(default=1400, ge=128, le=8192)

    @model_validator(mode="after")
    def validate_connection(self):
        self.base_url = self.base_url.strip().rstrip("/")
        self.model = self.model.strip()
        if self.kind == "disabled":
            return self
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Use an HTTP(S) base URL without credentials, query, or fragment.")
        try:
            _ = url.port
        except ValueError as exc:
            raise ValueError("Invalid provider port.") from exc
        if url.path.rstrip("/") != "/v1":
            raise ValueError("Use the provider base URL ending in /v1, not /chat/completions.")
        if self.kind == "lmstudio" and url.hostname not in LOCAL_HOSTS:
            raise ValueError(
                "LM Studio mode requires a local endpoint. Use API mode for a remote service."
            )
        if self.kind == "api" and (url.scheme != "https" or url.hostname in LOCAL_HOSTS):
            raise ValueError("Remote API mode requires an HTTPS endpoint.")
        if self.context_tokens <= self.max_output_tokens + 1024:
            raise ValueError("Context size must exceed output tokens by more than 1024 tokens.")
        return self


class ProviderFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def provider_failure(exc):
    if isinstance(exc, httpx.TimeoutException):
        return ProviderFailure(
            "provider_timeout",
            "The model timed out. Load the model or increase the timeout in Model setup.",
        )
    if isinstance(exc, httpx.ConnectError):
        return ProviderFailure(
            "provider_unreachable",
            "Cannot reach the model server. Start LM Studio's local server or check the API URL.",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return ProviderFailure(
                "provider_auth", "The provider rejected authentication. Check its API key."
            )
        if status == 429:
            return ProviderFailure(
                "provider_busy", "The provider is busy or rate limited. Try again shortly."
            )
        if status in {400, 404, 422}:
            return ProviderFailure(
                "provider_contract",
                "The provider rejected the model or request format. Check the model ID, context size, and JSON format.",
            )
    return ProviderFailure(
        "provider_failed",
        "The provider could not complete this request. Retrieved sources remain available.",
    )


class ProviderManager:
    def __init__(self, settings):
        self.initial = settings
        self.path = settings.data_dir / "provider.json"
        self.lock = threading.RLock()
        self.value = None
        self.last_check = None
        if self.path.exists():
            try:
                self.value = ProviderInput.model_validate_json(self.path.read_text())
            except (ValueError, OSError):
                raise ValueError(
                    "Saved provider settings are invalid or unreadable. Repair or remove data/provider.json before restarting."
                ) from None
        elif settings.llm_url:
            self.value = ProviderInput(
                kind=settings.llm_kind,
                base_url=settings.llm_url,
                model=settings.llm_model,
                api_key=settings.llm_key,
                response_format=settings.llm_format,
                timeout_seconds=settings.llm_timeout,
                context_tokens=settings.llm_context_tokens,
                max_output_tokens=settings.llm_max_tokens,
            )

    def snapshot(self):
        with self.lock:
            value = self.value or ProviderInput(kind="disabled")
            return replace(
                self.initial,
                llm_url=value.base_url if value.kind != "disabled" else "",
                llm_model=value.model if value.kind != "disabled" else "",
                llm_key=value.api_key or "",
                llm_kind=value.kind,
                llm_format=value.response_format,
                llm_timeout=value.timeout_seconds,
                llm_context_tokens=value.context_tokens,
                llm_max_tokens=value.max_output_tokens,
            )

    def public(self):
        with self.lock:
            value = self.value or ProviderInput()
            return {
                **value.model_dump(exclude={"api_key", "clear_key"}),
                "key_configured": bool(value.api_key),
                "configured": bool(self.value and value.kind != "disabled" and value.model),
                "last_check": self.last_check,
                "settings_source": "saved"
                if self.path.exists()
                else "environment"
                if self.initial.llm_url
                else "default",
                "privacy": "local"
                if value.kind == "lmstudio"
                else "remote"
                if value.kind == "api"
                else "disabled",
            }

    def resolved(self, value):
        value = value.model_copy(deep=True)
        with self.lock:
            # Never transfer a saved credential to a changed endpoint.
            same_endpoint = (
                self.value
                and self.value.kind == value.kind
                and self.value.base_url == value.base_url
            )
            if value.clear_key:
                value.api_key = ""
            elif value.api_key is None:
                value.api_key = self.value.api_key if same_endpoint else ""
        value.clear_key = False
        return value

    def save(self, value):
        with self.lock:
            value = self.resolved(value)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".provider-", dir=self.path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(value.model_dump(), stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if Path(name).exists():
                    Path(name).unlink()
            self.value = value
            self.last_check = None
            return self.public()

    async def models(self, value):
        value = self.resolved(value)
        if value.kind == "disabled":
            return []
        headers = {"Authorization": f"Bearer {value.api_key}"} if value.api_key else {}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10, connect=3), trust_env=False, follow_redirects=False
            ) as client:
                async with client.stream(
                    "GET", value.base_url + "/models", headers=headers
                ) as response:
                    response.raise_for_status()
                    parts, size = [], 0
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > 256_000:
                            raise ProviderFailure(
                                "provider_contract", "The model list exceeds the response limit."
                            )
                        parts.append(part)
                data = json.loads(b"".join(parts)).get("data")
                if not isinstance(data, list):
                    raise ValueError("Invalid models response")
                return sorted(
                    {m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)}
                )[:200]
        except httpx.HTTPError as exc:
            raise provider_failure(exc) from exc
        except (ValueError, AttributeError, TypeError) as exc:
            raise ProviderFailure(
                "provider_contract", "The server did not return a compatible model list."
            ) from exc
