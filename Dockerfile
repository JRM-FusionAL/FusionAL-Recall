FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY recall/ ./recall/
COPY .env.example .env.example

# Data volume mount point
RUN mkdir -p /data

EXPOSE 8107

ENV RECALL_HOST=0.0.0.0
ENV RECALL_PORT=8107
ENV RECALL_DB_PATH=/data/recall.db

CMD ["python", "-m", "recall.server"]
