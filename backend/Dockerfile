ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5
FROM ${UV_IMAGE} AS uvtool
FROM python:3.12-slim-bookworm AS native

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

# uv:Apache-2.0 / MIT
# Unmodified dynamic Pango/GLib/HarfBuzz libraries; Debian notices stay in image.
ARG APT_MIRROR=https://deb.debian.org/debian
ARG APT_SECURITY_MIRROR=https://deb.debian.org/debian-security
RUN sed -i "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g; s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*

FROM native AS dependencies
COPY --from=uvtool /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY wheelhouse /opt/wheelhouse
ARG UV_OFFLINE=
# 只安装生产依赖(不含 dev 组,因此不含 pgserver 等测试用包)
RUN if [ -n "$UV_OFFLINE" ]; then \
      uv venv .venv --python /usr/local/bin/python3 && \
      uv pip install --python .venv/bin/python --require-hashes --no-index \
        --find-links /opt/wheelhouse -r /opt/wheelhouse/requirements-linux.txt; \
    else uv sync --frozen --no-dev --no-install-project; fi
# Pyphen's code is tri-licensed; this distribution elects MPL-1.1. Its unused
# dictionaries have separate licenses and are deliberately not distributed.
RUN .venv/bin/python -c "import pathlib,pyphen,shutil; p=pathlib.Path(pyphen.__file__).parent/'dictionaries'; shutil.rmtree(p); p.mkdir()"

FROM native AS base
WORKDIR /app
COPY --from=dependencies /app/.venv /app/.venv
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN useradd --system --uid 10001 appuser \
    && mkdir -p /data/archive /tmp/cyshuhe-cache/fontconfig \
    && chown -R appuser /data /tmp/cyshuhe-cache
ENV CYSH_ARCHIVE_DIR=/data/archive \
    FONTCONFIG_FILE=/app/app/analysis/fonts/fontconfig.xml \
    XDG_CACHE_HOME=/tmp/cyshuhe-cache \
    PATH=/app/.venv/bin:$PATH
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
