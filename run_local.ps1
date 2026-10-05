# 本地起 st-shim（Windows）。云端部署请用 deploy_cloud.sh 或 Dockerfile。
#
# 例：
#   .\run_local.ps1 -Upstream "https://api.example.com/v1" -ApiKey "sk-xxxx"
#   然后在 Tavo 里把接口地址填成 http://127.0.0.1:8787/v1
#
# 先自检（推荐）：看上游到底是按什么拦你
#   python st_probe.py --upstream "https://api.example.com/v1" --key "sk-xxxx" --model "你的模型名"

param(
  [Parameter(Mandatory = $true)][string]$Upstream,
  [int]$Port = 8787,
  [string]$ApiKey = "",
  [string]$ClientToken = "",
  [string]$StVersion = "1.13.4",
  [string[]]$Header = @(),
  [string[]]$JsonField = @()
)

$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 找一个“真能跑”的 Python：Windows 上 python.exe 可能只是 Microsoft Store 的假别名
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
if (-not $py) { throw "找不到可用的 Python 3。请安装 Python 3（勾选 Add to PATH）。" }

$argv = @(
  (Join-Path $dir "st_shim.py"),
  "--upstream", $Upstream,
  "--host", "127.0.0.1",
  "--port", "$Port",
  "--st-version", $StVersion,
  "--log", (Join-Path $dir "st-shim.log"),
  "--verbose"
)
if ($ApiKey)      { $argv += @("--api-key", $ApiKey) }
if ($ClientToken) { $argv += @("--client-token", $ClientToken) }
foreach ($h in $Header)    { $argv += @("--header", $h) }
foreach ($j in $JsonField) { $argv += @("--json-field", $j) }

Write-Host "启动：$py $($argv -join ' ')" -ForegroundColor Cyan
Write-Host "Tavo 接口地址填： http://127.0.0.1:$Port/v1" -ForegroundColor Green
Write-Host "健康检查：       http://127.0.0.1:$Port/__stshim/health" -ForegroundColor Green
Write-Host "按 Ctrl+C 停止。`n"
& $py @argv
