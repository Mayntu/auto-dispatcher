# Backend: every service in one process (python -m app.all, BUS=memory) — the configuration the demo and all
# checks run on (CLAUDE.md §5, §23). CP-SAT runs in a process pool inside this container.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# dependencies from pyproject.toml first: this layer is rebuilt only when they change
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/req.txt \
 && pip install -r /tmp/req.txt

COPY app ./app
COPY data ./data
COPY web-mvp ./web-mvp

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["python", "-m", "app.all"]
