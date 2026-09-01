# Pinned by digest, not by tag: `3.12-slim` is mutable, so an unpinned build
# is not reproducible and a rebuild can silently change the base OS.
# Dependabot moves this pin (see .github/dependabot.yml).
FROM python:3.14-slim@sha256:656d12e70054d5fda18a045e2494c96701e9792dd1445f95b3d038df954f57e9

LABEL org.opencontainers.image.source="https://github.com/Franion03/home-harness" \
      org.opencontainers.image.description="OpenAPI tool server: Home Assistant, Google Calendar, Google Tasks, body metrics"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# The harness is stateless: no volume, nothing writable at runtime.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin harness && \
    chown -R harness:harness /app
USER 10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
