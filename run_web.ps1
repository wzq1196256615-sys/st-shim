# 本地起 st-shim 多站点版（带网页控制台，Windows）
#
# 例：
#   .\run_web.ps1
#   然后浏览器打开 http://127.0.0.1:8787/ 加站点、切站点
#   Tavo 里接口地址固定填 http://127.0.0.1:8787/v1
#
# 公网部署请务必带 -AdminToken，否则任何人都能打开控制台改你的配置：
#   .\run_web.ps1 -AdminToken "自己起一个长随机串" -Host 0.0.0.0

param(
  [int]$Port = 8787,
  [string]$BindHost = "127.0.0.1",
  [string]$AdminToken = "",
  [string]$Upstream = "",
  [string]$ApiKey = "",
  [switch]$Verbose
)

$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 找一个“真能跑”的 Python：Windows 上 python.exe 可能只是 Microsoft Store 的假别名，
# 调用它只会打印一句提示，所以必须试跑一次再认。
function Test-Python([string]$exe) {
  if (-not $exe) { return $false }
  if (-not (Get-Command $exe -ErrorAction SilentlyContinue) -and -not (Test-Path $exe -PathType Leaf)) { return $false }
  $old = $ErrorActionPreference
  $ErrorActionPreference = "SilentlyContinue"   # 假别名会往 stderr 喷提示，别被当成致命错误
  try {
    $null = & $exe -c "print(1)" 2>$null
    return ($LASTEXITCODE -eq 0)
  } finally {
    $ErrorActionPreference = $old
  }
}
$py = $null
foreach ($cand in @("python", "python3", "py",
                    "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe")) {
  if (Test-Python $cand) { $py = $cand; break }
}
if (-not $py) { throw "找不到可用的 Python 3。请安装 Python 3（勾选 Add to PATH），或把完整路径填进本脚本的候选列表。" }

$argv = @(
  (Join-Path $dir "st_shim_web.py"),
  "--host", $BindHost,
  "--port", "$Port",
  "--profiles", (Join-Path $dir "profiles.json"),
  "--log", (Join-Path $dir "st-shim.log")
)
if ($AdminToken) { $argv += @("--admin-token", $AdminToken) }
if ($Upstream)   { $argv += @("--upstream", $Upstream) }
if ($ApiKey)     { $argv += @("--api-key", $ApiKey) }
if ($Verbose)    { $argv += "--verbose" }

Write-Host "控制台：  http://127.0.0.1:$Port/" -ForegroundColor Cyan
Write-Host "Tavo 填： http://127.0.0.1:$Port/v1   （切换站点不用改这里）" -ForegroundColor Green
if ($AdminToken) { Write-Host "控制台口令已启用（网址后加 ?token=... 或在页面顶部填）" -ForegroundColor Yellow }
Write-Host "按 Ctrl+C 停止。`n"
& $py @argv
