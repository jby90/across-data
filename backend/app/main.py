from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.iam.router import admin_router, auth_router, user_router


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="车研数合 API",
        version="0.1.0",
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
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/api/health", tags=["system"])
    async def health():
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(user_router)
    return app


app = create_app()
