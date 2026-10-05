#!/usr/bin/env bash
# st-shim 云服务器一键部署（Linux / systemd）—— 带网页控制台的多站点版
#
# 用法：
#   sudo bash deploy_cloud.sh --admin-token 面板口令 [--upstream https://api.example.com/v1 --api-key sk-xxx]
#
# 说明：
#   装到 /opt/st-shim，注册 systemd 服务 st-shim。
#   控制台：   http://<公网IP>:8787/          （建议加 --admin-token）
#   Tavo 填：  http://<公网IP>:8787/v1        （切换站点不用改客户端）
#   指定站点： http://<公网IP>:8787/p/<站点id>/v1
set -euo pipefail

UPSTREAM=""
API_KEY=""
CLIENT_TOKEN=""
ADMIN_TOKEN=""
PORT=8787
ST_VERSION="1.13.4"
EXTRA=""

usage() {
  cat <<'EOF'
参数：
  --admin-token  控制台口令（公网部署必填，否则任何人都能改你的站点配置）
  --upstream     可选：首次启动就建一个默认站点，例如 https://api.example.com/v1
  --api-key      可选：配合 --upstream 一起用的 key
  --client-token 可选：默认站点的客户端令牌（Tavo 的 API Key 填它）
  --port         监听端口，默认 8787
  --st-version   伪装用的酒馆版本号，默认 1.13.4
  --extra        追加给脚本的原始参数，例如 --extra '--verbose'
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --upstream) UPSTREAM="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --client-token) CLIENT_TOKEN="$2"; shift 2 ;;
    --admin-token) ADMIN_TOKEN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --st-version) ST_VERSION="$2"; shift 2 ;;
    --extra) EXTRA="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1"; usage; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "请用 root 或 sudo 运行"; exit 1; }
if [[ -z "$ADMIN_TOKEN" ]]; then
  echo "警告：没设 --admin-token，控制台在公网上任何人都能打开并改配置。"
  read -r -p "仍然继续？(y/N) " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "未找到 python3，请先安装：apt install -y python3"; exit 1; }

echo "==> 安装到 /opt/st-shim"
install -d -m 755 /opt/st-shim
install -m 644 "$SRC_DIR/st_shim.py" /opt/st-shim/st_shim.py
install -m 644 "$SRC_DIR/st_shim_web.py" /opt/st-shim/st_shim_web.py

echo "==> 写环境文件 /etc/st-shim.env（600）"
umask 077
cat > /etc/st-shim.env <<EOF
# st-shim 配置：改完执行 systemctl restart st-shim
PORT=${PORT}
ST_VERSION=${ST_VERSION}
ADMIN_TOKEN=${ADMIN_TOKEN}
UPSTREAM=${UPSTREAM}
API_KEY=${API_KEY}
CLIENT_TOKEN=${CLIENT_TOKEN}
EXTRA_ARGS=${EXTRA}
EOF
chmod 600 /etc/st-shim.env

echo "==> 写 systemd 服务"
cat > /etc/systemd/system/st-shim.service <<EOF
[Unit]
Description=st-shim: rewrite client requests into SillyTavern format (multi-site + web console)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/st-shim.env
WorkingDirectory=/opt/st-shim
ExecStart=/bin/sh -c 'exec ${PY} /opt/st-shim/st_shim_web.py \\
  --host 0.0.0.0 --port "\$PORT" --st-version "\$ST_VERSION" \\
  --profiles /opt/st-shim/profiles.json --log /opt/st-shim/st-shim.log \\
  \${ADMIN_TOKEN:+--admin-token "\$ADMIN_TOKEN"} \\
  \${UPSTREAM:+--upstream "\$UPSTREAM"} \\
  \${API_KEY:+--api-key "\$API_KEY"} \\
  \${CLIENT_TOKEN:+--client-token "\$CLIENT_TOKEN"} \$EXTRA_ARGS'
Restart=always
RestartSec=3
User=root
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable st-shim >/dev/null
systemctl restart st-shim
sleep 2

echo "==> 健康检查"
if curl -fsS "http://127.0.0.1:${PORT}/__stshim/health"; then
  echo
  echo "OK：服务已在 ${PORT} 端口运行"
else
  echo "健康检查失败，看日志： journalctl -u st-shim -n 50 --no-pager"
  exit 1
fi

if command -v ufw >/dev/null 2>&1; then
  echo "==> 放行 ufw ${PORT}/tcp"
  ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
fi

PUBIP="$(curl -fsS --max-time 5 https://api.ipify.org || echo '<云服务器公网IP>')"
cat <<EOF

================= 下一步 =================
1) 云服务商安全组里放行 TCP ${PORT}
2) 浏览器打开控制台，加站点、填上游地址和 key、点「测试」：
       http://${PUBIP}:${PORT}/${ADMIN_TOKEN:+?token=${ADMIN_TOKEN}}
3) Tavo 里填（固定这一个，切换站点不用改）：
      接口地址 Base URL : http://${PUBIP}:${PORT}/v1
      API Key          : ${CLIENT_TOKEN:-<留空或填你给该站点设的客户端令牌>}
4) 想固定走某个站点（不做切换）：
      http://${PUBIP}:${PORT}/p/<站点id>/v1
5) 日志： tail -f /opt/st-shim/st-shim.log   /   journalctl -u st-shim -f
==========================================
EOF
