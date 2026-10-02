# One image runs everything: the migration job, the Dagster web UI, the Dagster daemon and the CLI.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY dagster ./dagster
RUN uv sync --frozen --no-dev

# A fixed unprivileged user, so the host can give it ownership of the secrets and media folders.
RUN useradd --uid 10001 --create-home dk && mkdir -p /dagster_home /srv/media /srv/public \
    && chown -R dk /dagster_home /srv/media /srv/public
USER dk
ENV PATH="/app/.venv/bin:$PATH" DAGSTER_HOME=/dagster_home TZ=Europe/Berlin
