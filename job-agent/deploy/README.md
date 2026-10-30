# 部署与下线

线上地址：https://job.wyphub.com（Hetzner VPS，2 核 / 4G，和 OCR 演示 `ocr.wyphub.com` 同一台）

## 架构

```
浏览器 → Cloudflare（HTTPS）→ Tunnel bill-ocr → cloudflared（VPS）→ 127.0.0.1:8091 uvicorn（job-agent.service）
```

| 选择 | 原因 |
|---|---|
| 复用 OCR 演示的 Tunnel，只加一条路由 | 服务器已登录 Cloudflare，不用再开端口、不用再装 cloudflared |
| systemd + uv，不用 Docker | 和 OCR 演示一致；机器上没有 Docker |
| 专用用户 `jobagent`、只听 127.0.0.1 | 服务出问题也碰不到 root 和其他服务 |
| 单进程 | 免费额度记在内存里，多进程会各记各的 |
| `--no-proxy-headers` + 读 `CF-Connecting-IP` | 经 Tunnel 后来源 IP 全是 127.0.0.1；真实 IP 只从 Cloudflare 的头里取，且只信来自本机的请求 |
| `MemoryMax=1200M`、`Nice=5` | 同机还有 OCR 演示、VPN；超限只杀本服务 |
| 前端在本机打包，服务器上不装 Node | 少装一样也少删一样 |
| 不推 GitHub，本机打包直传 | 代码暂不公开；包里不含简历、评测数据、缓存 |

## 线上的成本控制

| 项 | 默认值 | 改法（`.env`） |
|---|---|---|
| 每个 IP 每天免费处理的 JD 数（深度准备一次算 3 个） | 20 | `JOB_FREE_PER_DAY` |
| 全站每天免费总量（换 IP 也绕不过） | 200，约 6 元封顶 | `JOB_FREE_TOTAL_PER_DAY` |
| 简历 / JD / 上传文件长度 | 2 万字 / 1 万字 / 5MB | 代码常量 |
| 深度准备存档保留 | 7 天 | 代码常量 `KEEP_DAYS` |

用户在"设置"里填自己的 DeepSeek Key 就不占免费额度（截图识别除外：它用服务器的通义 Key）。

## 部署 / 更新

本机（PowerShell，在 `job-agent` 目录下）一条命令：

```powershell
.\deploy\push.ps1
```

它会：打包前端 → 打包代码（不含 `.venv`、简历、评测、缓存）→ 从 `.env` 只挑线上要用的几行 → scp 上传 → 在服务器上跑 `setup.sh`。
`setup.sh` 可以重复执行：第一次安装，之后就是更新（保留公司缓存和存档）。

常用排查（服务器上）：

```bash
systemctl status job-agent
journalctl -u job-agent -n 50
curl -s 127.0.0.1:8091/api/health
```

## 下线

```bash
bash /opt/job-agent/deploy/teardown.sh
```

只删 `/root/job-agent-installed.txt` 里记过的：服务、Tunnel 里本站的那条路由、`/opt/job-agent`（含 `.env`、缓存、存档）、用户 `jobagent`。
还要手动做：Cloudflare 面板 → wyphub.com → DNS，删掉 `job` 这条 CNAME。

**注意**：Tunnel 属于 OCR 演示。如果先下线 OCR 演示（它的 teardown 会删掉整个 Tunnel），本站也会断，需要另建 Tunnel。
