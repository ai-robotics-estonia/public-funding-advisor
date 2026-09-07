# AI Funding Advisor — single FastAPI process serving its own static frontend.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/data

WORKDIR /app

# Dependencies first so edits to the source do not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY scraper/ ./scraper/
COPY tools/ ./tools/
COPY frontend/ ./frontend/
# Baked in so the image runs standalone; compose bind-mounts the same path
# read-only so measures can be edited without a rebuild.
COPY rahastusmeetmed/ ./rahastusmeetmed/

# Unprivileged runtime user. /data is the only writable path (the container
# filesystem itself is mounted read-only by compose).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown -R app:app /data
USER app

EXPOSE 8000

# One worker on purpose: the rate limiter and the scraper scheduler both keep
# per-process state, and SQLite prefers a single writer. Concurrency comes from
# Starlette's threadpool, capped further by LLM_MAX_CONCURRENCY.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
