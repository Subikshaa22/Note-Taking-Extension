# One image definition, two targets: `gateway` (API only) and `worker` (adds ffmpeg and the
# Deno JavaScript runtime that yt-dlp needs to solve YouTube's player challenges).

FROM python:3.12-slim AS python-deps
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && useradd --create-home --uid 10001 app


FROM python-deps AS gateway
COPY notetaker ./notetaker
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status != 200)"
CMD ["uvicorn", "notetaker.gateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]


FROM python-deps AS worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno
COPY notetaker ./notetaker
USER app
CMD ["python", "-m", "notetaker.worker.main"]
