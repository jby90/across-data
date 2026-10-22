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

    # 采集:原文归档目录、礼貌抓取参数
    archive_dir: str = "./data/archive"
    crawl_user_agent: str = (
        "CheyanShuheBot/0.1 (+compliance research; contact: set CYSH_CRAWL_CONTACT)"
    )
    crawl_delay_seconds: float = 2.0
    crawl_timeout_seconds: float = 30.0
    crawl_max_bytes: int = 100 * 1024 * 1024
    worker_poll_seconds: int = 60
    # 允许「域名解析结果」落入的内网网段(默认空=一律拦截)。仅用于开发机:
    # 代理软件的 fake-IP 模式会把所有域名解析到 198.18.0.0/15,此时可设为 ["198.18.0.0/15"]
    crawl_allow_resolved_cidrs: list[str] = []

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url 必须使用 postgresql+asyncpg:// 驱动")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
