# 本机运行（PowerShell，在 job-agent 目录下）：打包 → 上传 → 在服务器上执行 setup.sh。
#   .\deploy\push.ps1
# 只上传运行需要的文件：不含简历、评测数据、本地缓存、.venv、node_modules。
# .env 只挑线上要用的几行（不含 OpenRouter 等评测才用的 Key），不在终端打印内容。
$ErrorActionPreference = "Stop"
$Server = $env:JOB_AGENT_SERVER   # 如 root@1.2.3.4；不写进仓库
if (-not $Server) { throw '先设置服务器地址：$env:JOB_AGENT_SERVER = "root@<服务器IP>"' }
# SSH / scp 不能走 Clash 代理（否则报错且提示完全不指向代理）
$env:HTTPS_PROXY = ""; $env:HTTP_PROXY = ""; $env:https_proxy = ""; $env:http_proxy = ""

Push-Location web
npm run build
Pop-Location

$tmp = Join-Path $env:TEMP "job-agent-deploy"
New-Item -ItemType Directory -Force $tmp | Out-Null
$pkg = Join-Path $tmp "job-agent.tar.gz"
$envFile = Join-Path $tmp "job-agent.env"

tar --exclude=__pycache__ -czf $pkg pyproject.toml uv.lock .python-version README.md src deploy web/dist
$keep = '^(DEEPSEEK_API_KEY|DEEPSEEK_BASE_URL|ZHIPUAI_API_KEY|DASHSCOPE_API_KEY|TAVILY_API_KEY|LANGFUSE_\w+|JOB_\w+|MCP_PUBLIC_TOKEN)='
# 线上 MCP 用单独的令牌（本机 Dify 用的 MCP_TOKEN 不上传），到服务器上改名为 MCP_TOKEN
Get-Content .env -Encoding utf8 | Where-Object { $_ -match $keep } | ForEach-Object { $_ -replace '^MCP_PUBLIC_TOKEN=', 'MCP_TOKEN=' } |
    Set-Content $envFile -Encoding utf8NoBOM
Write-Host ("打包完成：{0:N0} KB；.env 保留 {1} 行" -f ((Get-Item $pkg).Length / 1KB), (Get-Content $envFile).Count)

scp $pkg $envFile "${Server}:/root/"
Remove-Item -Recurse -Force $tmp
# 先从包里取出 setup.sh 再执行（tr 去掉 Windows 换行符，免得 bash 报 $'\r': command not found）
ssh $Server "tar -xzf /root/job-agent.tar.gz -O deploy/setup.sh | tr -d '\r' > /root/job-agent-setup.sh && bash /root/job-agent-setup.sh; rm -f /root/job-agent-setup.sh"
