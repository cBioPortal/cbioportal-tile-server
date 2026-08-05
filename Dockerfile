FROM python:3.13.5-slim-bookworm

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv

WORKDIR /app

# libopenslide-dev is NOT required — tiffslide is pure Python.
# We only need the compression libs used by tifffile/Pillow.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libjpeg-turbo-progs \
    libzstd-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer — only reruns when pyproject.toml/uv.lock change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project
COPY app/ ./app/
RUN uv sync --frozen --no-dev

# Add venv to PATH so binaries are available without `uv run`
ENV PATH="/app/.venv/bin:$PATH"

RUN useradd --no-create-home --shell /bin/false appuser
# A fresh named volume mounted at this path inherits this ownership.
RUN mkdir -p /cache/slide-blocks /cache/prometheus && chown -R appuser:appuser /cache
ENV PROMETHEUS_MULTIPROC_DIR=/cache/prometheus
USER appuser

EXPOSE 8080

# Memory-bound defaults for a 4 GiB pod. Increase only after measuring RSS
# under representative slide load.
CMD ["gunicorn", "-c", "python:app.gunicorn_conf", "app.main:app"]
