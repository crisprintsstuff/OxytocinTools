FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
# Database and the GitHub App private key live here (see docker-compose.yml).
VOLUME /app/data
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, sqlite3; p = os.getenv('DATABASE_PATH', 'data/tracker.db'); assert os.path.exists(p); sqlite3.connect(p, timeout=2).execute('SELECT 1')" || exit 1
CMD ["python", "-m", "bot"]
