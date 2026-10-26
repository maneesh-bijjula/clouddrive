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


@lru_cache
def settings() -> Settings:
    return Settings()
