from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。所有值来自环境变量(前缀 CYSH_)或 .env,密钥不设默认值。"""

    model_config = SettingsConfigDict(env_prefix="CYSH_", env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://cyshuhe:change-me@localhost:5432/cyshuhe"
    jwt_secret: str = Field(min_length=32)
    jwt_ttl_minutes: int = 60
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:5174"]

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url 必须使用 postgresql+asyncpg:// 驱动")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
