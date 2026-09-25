FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 启动时自动完成库结构核对（空库建表；半旧库拒绝就绪，见 app/schema_bootstrap.py 与 app/main.py 的 lifespan）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
