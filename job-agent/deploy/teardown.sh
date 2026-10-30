#!/usr/bin/env bash
# 下线：在服务器上以 root 运行。只删除 setup.sh 装过的东西；Tunnel 本身属于 OCR 演示，只删本站那条路由。
set -uo pipefail

HOST=job.wyphub.com
CF=/etc/cloudflared/config.yml
MANIFEST=/root/job-agent-installed.txt
has() { grep -qxF "$1" "$MANIFEST" 2>/dev/null; }

echo "== 1. 停止并删除服务"
systemctl disable --now job-agent job-mcp 2>/dev/null
rm -f /etc/systemd/system/job-agent.service /etc/systemd/system/job-mcp.service
systemctl daemon-reload

echo "== 2. 从 Tunnel 配置里删掉本站路由（先删 /mcp 那条三行规则，再删通用规则的 hostname + service 两行）"
if has mcp-ingress && [ -f "$CF" ]; then
  awk -v h="  - hostname: $HOST" 'buf!=""{ if($0=="    path: ^/mcp"){skip=1; buf=""; next} print buf; buf="" }
    skip==1{skip=0; next} $0==h{buf=$0; next} {print} END{if(buf!="")print buf}' "$CF" > "$CF.tmp" && mv "$CF.tmp" "$CF"
  rm -f "$CF.bak-job-mcp"
fi
if has ingress && [ -f "$CF" ]; then
  sed -i "\#  - hostname: $HOST#{N;d}" "$CF"
  cloudflared tunnel ingress validate && systemctl restart cloudflared
  rm -f "$CF.bak-job-agent"
fi

echo "== 3. 删除代码、密钥、依赖、缓存和存档"
rm -rf /opt/job-agent
if has user; then userdel -r jobagent 2>/dev/null; rm -rf /home/jobagent; fi

echo "== 4. 检查残留（下面应全部为空/不存在）"
systemctl list-unit-files | grep job-agent || echo "服务: 无"
id jobagent 2>/dev/null || echo "用户 jobagent: 无"
ls -d /opt/job-agent /home/jobagent 2>/dev/null || echo "目录: 无"
grep -n "$HOST" "$CF" 2>/dev/null || echo "Tunnel 路由: 无"
ss -ltn | grep -q ":8091 " && echo "!! 8091 仍在监听" || echo "端口 8091: 无"
ss -ltn | grep -q ":8092 " && echo "!! 8092 仍在监听" || echo "端口 8092: 无"
rm -f "$MANIFEST"
echo "完成。还要手动：Cloudflare 面板删 $HOST 的 DNS 记录；如果部署用了单独的 DeepSeek Key，去控制台删掉。"
