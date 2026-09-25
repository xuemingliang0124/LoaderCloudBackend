FROM docker.m.daocloud.io/library/python:3.12.14-slim

# 时区：与 mysql/agent 对齐为 Asia/Shanghai，保证 datetime.now()/run_no 时间戳为北京墙钟
ENV TZ=Asia/Shanghai
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# tests + pytest.ini 打入镜像：CI 接口测试直接复用本镜像，保证测试代码与
# app 代码同版本（无需挂载 tests，不会因下游重新检出代码导致版本漂移）
COPY tests ./tests
COPY pytest.ini .
COPY alembic ./alembic
COPY alembic.ini .
COPY entrypoint.sh /entrypoint.sh
# sed 去 CRLF：兼容 Windows 检出（git autocrlf）导致的脚本换行问题
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 8000
# 入口先跑 alembic upgrade head 再启动服务（exec "$@" 承接 CMD）
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
