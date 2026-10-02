FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先单独复制依赖清单，便于利用 Docker 分层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8080

# 容器内通过 uvicorn 提供 HTTP 服务；--reload 默认关闭，适合离线常驻
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
