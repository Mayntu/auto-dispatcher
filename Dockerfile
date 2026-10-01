# Single Python image for every backend service; the command differs per service (CLAUDE.md §23).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies (and the app package) first for better layer caching.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

# Runtime assets and data.
COPY data ./data
COPY tools ./tools
COPY migrations ./migrations
COPY web-mvp ./web-mvp

# `python -m app.<service>.main` runs the local ./app, so ROOT resolves to /app and finds data/.
EXPOSE 8000
CMD ["python", "-m", "app.all"]
