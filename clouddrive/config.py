from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOUDDRIVE_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://clouddrive:clouddrive@localhost:5435/clouddrive"
    redis_url: str = "redis://localhost:6382/0"
    s3_endpoint: str = "http://localhost:9000"
    s3_public_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "clouddrive"
    s3_secret_key: str = "clouddrive-local"
    s3_bucket: str = "clouddrive"
    s3_region: str = "us-east-1"
    max_file_bytes: int = Field(default=5_000_000_000, ge=1, le=5_000_000_000)
    part_bytes: int = Field(default=8 * 1024 * 1024, ge=5 * 1024 * 1024, le=64 * 1024 * 1024)
    signed_url_seconds: int = Field(default=900, ge=60, le=3600)
    max_active_uploads_per_workspace: int = Field(default=10, ge=1, le=100)
    max_active_uploads_global: int = Field(default=100, ge=1, le=10000)
    max_api_inflight: int = Field(default=64, ge=1, le=1024)
    db_pool_size: int = Field(default=8, ge=1, le=64)
    db_max_overflow: int = Field(default=8, ge=0, le=64)
    db_pool_timeout_seconds: float = Field(default=10, gt=0, le=60)
    admission_lock_timeout_seconds: float = Field(default=5, gt=0, le=30)
    metrics_key: str = Field(default="", max_length=200, repr=False)
    browser_active_files: int = Field(default=10, ge=1, le=10)
    browser_hash_workers: int = Field(default=2, ge=1, le=2)
    browser_global_parts: int = Field(default=10, ge=1, le=10)
    browser_parts_per_file: int = Field(default=3, ge=1, le=3)


@lru_cache
def settings() -> Settings:
    return Settings()
