FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/beamprotect.db \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

# 仅使用 Python 标准库，无第三方依赖
COPY app ./app
COPY tests ./tests
COPY scripts ./scripts

RUN mkdir -p /data \
    && python -m compileall -q app tests scripts

EXPOSE 8080

CMD ["python", "-m", "app.main"]
