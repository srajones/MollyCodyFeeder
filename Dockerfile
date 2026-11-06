FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    espeak-ng \
    tzdata \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watchdog/ ./watchdog/
COPY scripts/ ./scripts/
COPY feeder_cli.py ./feeder_cli.py
# Labelling page + the owner's committed labels (imported into the DB once; small text files only)
COPY ml/review_bundle.html ./ml/review_bundle.html
COPY ml/labels/ ./ml/labels/

# Run as an unprivileged user (uid 1000 so bind-mounted ./data and ./captures stay writable)
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin puppy \
    && mkdir -p /app/data /tmp/poor_puppy_captures \
    && chown -R puppy:puppy /app /tmp/poor_puppy_captures
USER puppy

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/kibble_history.db \
    CAPTURES_DIR=/tmp/poor_puppy_captures

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health >/dev/null || exit 1

CMD ["python", "-m", "watchdog.main"]
