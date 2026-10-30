#!/usr/bin/env bash
# 在服务器上以 root 运行：安装或更新求职 Agent。本机先用 deploy/push.ps1 把代码包和 .env 传到 /root/。
# 所有新建的东西都记进 MANIFEST，下线时 teardown.sh 只删清单里的，不碰机器上的其他服务。
# 公网入口复用 OCR 演示已有的 Cloudflare Tunnel（bill-ocr），只给它加一条 job.wyphub.com 的路由。
set -euo pipefail

APP=/opt/job-agent
HOST=job.wyphub.com
PORT=8091
TUNNEL=bill-ocr
CF=/etc/cloudflared/config.yml
MANIFEST=/root/job-agent-installed.txt
note() { grep -qxF "$1" "$MANIFEST" 2>/dev/null || echo "$1" >> "$MANIFEST"; }

# 1. 专用用户：服务不用 root 跑
if ! id jobagent >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/jobagent --shell /usr/sbin/nologin jobagent
  note user
fi

# 2. 代码：解压覆盖（.cache 公司缓存、prep_out 存档、.venv 不在包里，更新时保留）
[ -f /root/job-agent.tar.gz ] || { echo "缺少 /root/job-agent.tar.gz"; exit 1; }
mkdir -p "$APP" && note app
tar -xzf /root/job-agent.tar.gz -C "$APP" && rm -f /root/job-agent.tar.gz

# 3. .env 只移动并收紧权限，不打印内容
if [ -f /root/job-agent.env ]; then
  install -m 600 /root/job-agent.env "$APP/.env" && rm -f /root/job-agent.env
fi
[ -f "$APP/.env" ] || { echo "缺少 $APP/.env"; exit 1; }
chown -R jobagent:jobagent "$APP"
chmod 700 "$APP"   # 存档、缓存、.env 都在里面，只给 jobagent 看

# 4. 依赖（uv 装在 jobagent 自己的目录里，下线删用户即删干净）
sudo -u jobagent -H bash -c 'command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u jobagent -H bash -c "cd $APP && ~/.local/bin/uv sync --no-dev --frozen"

# 5. systemd 服务
install -m 644 "$APP/deploy/job-agent.service" /etc/systemd/system/job-agent.service
note service
systemctl daemon-reload
systemctl enable job-agent >/dev/null
systemctl restart job-agent

# 6. Tunnel 路由：在兜底的 404 规则前插入本站；DNS 记录（CNAME）由 cloudflared 创建
if ! grep -q "hostname: $HOST" "$CF"; then
  cp "$CF" "$CF.bak-job-agent"
  sed -i "s#^  - service: http_status:404#  - hostname: $HOST\n    service: http://127.0.0.1:$PORT\n  - service: http_status:404#" "$CF"
  cloudflared tunnel ingress validate
  systemctl restart cloudflared
  note ingress
fi
if ! grep -qxF dns "$MANIFEST"; then
  cloudflared tunnel route dns "$TUNNEL" "$HOST" && note dns
fi

for i in $(seq 1 30); do
  curl -sf "127.0.0.1:$PORT/api/health" && echo && echo "已启动：https://$HOST" && exit 0
  sleep 2
done
echo "启动失败，看日志：journalctl -u job-agent -n 50"; exit 1
