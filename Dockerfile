FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home yosuu
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY yosuu/ ./yosuu/
COPY --from=frontend /build/dist ./frontend/dist
USER yosuu
EXPOSE 8000
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "yosuu.app:app", "--host", "0.0.0.0", "--no-access-log"]
