FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

# uv:Apache-2.0 / MIT
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
# 只安装生产依赖(不含 dev 组,因此不含 pgserver 等测试用包)
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN useradd --system --uid 10001 appuser \
    && mkdir -p /data/archive && chown -R appuser /data
ENV CYSH_ARCHIVE_DIR=/data/archive
USER appuser

EXPOSE 8000
CMD ["sh", "-c", "uv run --no-dev alembic upgrade head && uv run --no-dev uvicorn app.main:app --host 0.0.0.0 --port 8000"]
