# 本地 / 云服务器直接用 docker 跑（多站点 + 网页控制台）
#
#   docker build -t st-shim .
#   docker run -d --name st-shim --restart=always -p 8787:8787 \
#     -v st-shim-data:/app/data \
#     -e ADMIN_TOKEN=你的面板口令 -e UPSTREAM=https://api.example.com/v1 -e API_KEY=sk-xxxx \
#     st-shim
#
# 控制台： http://<IP>:8787/       Tavo 填： http://<IP>:8787/v1
# 说明：UPSTREAM 里如果带 /v1，客户端也发 /v1/... 不会重复拼接。

FROM python:3.12-alpine

WORKDIR /app
COPY st_shim.py /app/st_shim.py
COPY st_shim_web.py /app/st_shim_web.py
VOLUME /app/data

ENV PORT=8787 \
    ST_VERSION=1.13.4 \
    ADMIN_TOKEN="" \
    UPSTREAM="" \
    API_KEY="" \
    CLIENT_TOKEN="" \
    EXTRA_ARGS=""

EXPOSE 8787

CMD ["sh", "-c", "exec python /app/st_shim_web.py --host 0.0.0.0 --port \"$PORT\" --st-version \"$ST_VERSION\" --profiles /app/data/profiles.json --log /app/data/st-shim.log ${ADMIN_TOKEN:+--admin-token \"$ADMIN_TOKEN\"} ${UPSTREAM:+--upstream \"$UPSTREAM\"} ${API_KEY:+--api-key \"$API_KEY\"} ${CLIENT_TOKEN:+--client-token \"$CLIENT_TOKEN\"} $EXTRA_ARGS"]

