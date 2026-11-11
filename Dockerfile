FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATABASE_PATH=/data/tracker.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
VOLUME /data
CMD ["python", "-m", "bot"]
