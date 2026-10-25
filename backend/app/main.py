from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models_registry  # noqa: F401
from app.analysis.router import (
    admin_router as analysis_admin_router,
)
from app.analysis.router import (
    user_router as analysis_user_router,
)
from app.core.config import get_settings
from app.iam.router import admin_router, auth_router, user_router
from app.ingestion.router import router as documents_router
from app.knowledge.router import (
    admin_router as knowledge_admin_router,
)
from app.knowledge.router import (
    user_router as knowledge_user_router,
)
from app.sources.router import router as sites_router


@asynccontextmanager
async def _lifespan(_app):
    yield
    from app.analysis.events import close_hubs

    await close_hubs()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="车研数合 API",
        version="0.1.0",
        lifespan=_lifespan,
        # 生产环境不暴露交互式文档
        docs_url="/api/docs" if settings.env == "dev" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.env == "dev" else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )

    @app.get("/api/health", tags=["system"])
    async def health():
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(user_router)
    app.include_router(sites_router)
    app.include_router(documents_router)
    app.include_router(knowledge_admin_router)
    app.include_router(knowledge_user_router)
    app.include_router(analysis_admin_router)
    app.include_router(analysis_user_router)

    return app


app = create_app()
