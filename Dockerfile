# Build stage: resolve the locked environment with uv.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
COPY README.md ./
RUN uv sync --frozen --no-dev

# Runtime stage: slim python + the docker CLI (client only) for the verify
# sandbox, which talks to the HOST daemon via a mounted socket.
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://download.docker.com/linux/static/stable/$(uname -m | sed 's/arm64/aarch64/')/docker-27.3.1.tgz \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY src/ src/
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# Root: the mounted docker socket requires it (documented tradeoff for a
# local/portfolio deployment; a socket-proxy is the hardened alternative).
EXPOSE 8000
CMD ["uvicorn", "tvastr.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
