import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

RUNTIME_FINGERPRINT = hashlib.sha256(
    b"".join(p.name.encode() + p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))
).hexdigest()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    research_mode: Literal["live", "fixture"] = "live"
    database_url: str = "postgresql://research_app:research_local@localhost:15432/research"
    admin_database_url: str = "postgresql://postgres:postgres_local@localhost:15432/research"
    s3_endpoint: str = "http://localhost:19000"
    s3_access_key: str = "research_minio"
    s3_secret_key: str = "research_minio_local_secret"
    s3_bucket: str = "research-artifacts"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_thinking: Literal["enabled", "disabled"] = "enabled"
    deepseek_effort: str = "low"
    memory_api_key: str = ""
    memory_base_url: str = "https://api.deepseek.com"
    memory_model: str = ""
    memory_input_usd_per_million: float = 0.30
    memory_output_usd_per_million: float = 1.20
    memory_job_max_input_tokens: int = 8000
    memory_job_max_output_tokens: int = 2000
    memory_job_max_usd: float = 0.05
    memory_checkpoint_keep: int = 1000
    tavily_api_key: str = ""
    model_input_usd_per_million: float = 0.30
    model_output_usd_per_million: float = 1.20
    model_cache_usd_per_million: float = 0.006
    search_usd_per_call: float = 0.01
    live_campaign_usd: float = 10.0
    api_origin: str = "http://localhost:18000"
    parser_url: str | None = None
    embedding_url: str | None = None
    embedding_model: str = "Alibaba-NLP/gte-multilingual-base"
    embedding_revision: str = "9bbca17"
    embedding_dimensions: int = 768
    index_lease_seconds: int = 180
    index_poll_seconds: float = 0.5
    index_wait_seconds: int = 30
    retrieval_shadow: bool = False
    lease_seconds: int = 45
    heartbeat_seconds: int = 10
    max_upload_bytes: int = 20 * 1024 * 1024
    max_document_chars: int = 600000
    request_timeout: int = 90
    worker_id: str = "worker-local"
    academic_connector_timeout: float = 12.0
    academic_connector_attempts: int = 2
    academic_candidates_per_connector: int = 20
    academic_contact_email: str = ""
    academic_user_agent: str = "deep-research-agent/0.4 (scholarly metadata discovery)"
    openalex_api_key: str = ""
    ncbi_api_key: str = ""

    def execution_snapshot(self) -> dict:
        fields = [
            "deepseek_base_url",
            "deepseek_model",
            "deepseek_thinking",
            "deepseek_effort",
            "memory_base_url",
            "memory_model",
            "memory_job_max_input_tokens",
            "memory_job_max_output_tokens",
            "memory_job_max_usd",
            "model_input_usd_per_million",
            "model_output_usd_per_million",
            "model_cache_usd_per_million",
            "search_usd_per_call",
            "request_timeout",
            "embedding_model",
            "embedding_revision",
            "retrieval_shadow",
        ]
        return {
            "runtime_fingerprint": RUNTIME_FINGERPRINT,
            "workflow_version": "v1",
            "settings": {key: getattr(self, key) for key in fields},
        }


@lru_cache
def settings() -> Settings:
    return Settings()
