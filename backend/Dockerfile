# Enterprise RAG backend / worker image（同一镜像，compose 里用不同 command 区分 API 与 ARQ worker）
FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # 让 `app` 包直接从 /app 解析：config.py 依 __file__ 反推项目根（/），
    # 从而 knowledge_base_dir=/knowledge_base 等路径与 compose 挂载点对齐。
    PYTHONPATH=/app

# 先拷依赖清单与包元数据，利用层缓存
COPY pyproject.toml ./
# Alembic 迁移资产（migrate 一次性服务用）
COPY alembic.ini ./
COPY migrations ./migrations
# 应用源码
COPY app ./app
COPY eval ./eval

# 国内镜像源加速（大 wheel：pymupdf/numpy/grpcio 等）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ .

EXPOSE 8000

# 容器内健康检查命中存活探针（不依赖外部服务，秒回）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
