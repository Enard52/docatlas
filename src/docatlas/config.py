import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    embeddings: str = "bm25"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    llm_url: str = ""
    llm_model: str = ""
    llm_key: str = ""
    llm_kind: str = "lmstudio"
    llm_format: str = "json_schema"
    llm_timeout: int = 120
    llm_context_tokens: int = 8192
    llm_max_tokens: int = 1400
    api_key: str = ""
    rate_limit: int = 60
    max_upload_mb: int = 10
    min_dense_score: float = 0.55
    ingestion_timeout: int = 180
    worker_memory_mb: int = 1024

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.getenv("DOCATLAS_DATA_DIR", "data")),
            embeddings=os.getenv("DOCATLAS_EMBEDDINGS", "bm25"),
            embedding_model=os.getenv("DOCATLAS_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
            llm_url=os.getenv("DOCATLAS_LLM_URL", "").rstrip("/"),
            llm_model=os.getenv("DOCATLAS_LLM_MODEL", ""),
            llm_key=os.getenv("DOCATLAS_LLM_KEY", ""),
            llm_kind=os.getenv("DOCATLAS_LLM_KIND", "lmstudio"),
            llm_format=os.getenv("DOCATLAS_LLM_FORMAT", "json_schema"),
            llm_timeout=int(os.getenv("DOCATLAS_LLM_TIMEOUT", "120")),
            llm_context_tokens=int(os.getenv("DOCATLAS_LLM_CONTEXT_TOKENS", "8192")),
            llm_max_tokens=int(os.getenv("DOCATLAS_LLM_MAX_TOKENS", "1400")),
            api_key=os.getenv("DOCATLAS_API_KEY", ""),
            rate_limit=max(1, int(os.getenv("DOCATLAS_RATE_LIMIT", "60"))),
            max_upload_mb=max(1, int(os.getenv("DOCATLAS_MAX_UPLOAD_MB", "10"))),
            worker_memory_mb=max(128, int(os.getenv("DOCATLAS_WORKER_MEMORY_MB", "1024"))),
            ingestion_timeout=max(10, int(os.getenv("DOCATLAS_INGESTION_TIMEOUT", "180"))),
            min_dense_score=float(os.getenv("DOCATLAS_MIN_DENSE_SCORE", "0.55")),
        )
