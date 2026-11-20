FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY prompts/ prompts/

RUN useradd --create-home --uid 1000 agents \
    && mkdir -p /data \
    && chown agents:agents /data
USER agents

VOLUME ["/data"]

CMD ["python", "-m", "app.bot"]
