#Requires -Version 5.1
<#
.SYNOPSIS
  Authenticode 签名 Setup.exe / 极限词扫描.exe（消除或减轻 SmartScreen）。

  需要：Windows SDK signtool + 商业代码签名证书（OV/EV .pfx）。
  自签名证书过不了 SmartScreen，不要用。

  配置写在仓库根 config.ini.local（勿提交）：

  [codesign]
  ; 必填：pfx 路径（示例，勿提交真实路径到 Git）
  pfx_path = D:\secrets\codesign.pfx
  ; 必填：pfx 密码（写在 config.ini.local，勿提交）
  pfx_password =
  ; 可选：时间戳服务器
  timestamp_url = http://timestamp.digicert.com
  ; 可选：signtool 路径；空则自动找 Windows Kits
  signtool =
#>
param(
  [Parameter(Mandatory = $true)]
  [string[]]$Files
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$localIni = Join-Path $Root "config.ini.local"
if (-not (Test-Path $localIni)) {
  throw "缺少 $localIni 。请配置 [codesign] pfx_path / pfx_password 后再签名。"
}

$cfg = Get-Content -LiteralPath $localIni -Encoding UTF8
function Get-IniVal([string]$section, [string]$key) {
  $in = $false
  foreach ($line in $cfg) {
    $t = $line.Trim()
    if ($t -match '^\[(.+)\]$') {
      $in = ($Matches[1] -eq $section)
      continue
    }
    if (-not $in) { continue }
    if ($t -match ('^' + [regex]::Escape($key) + '\s*=\s*(.*)$')) {
      return $Matches[1].Trim()
    }
  }
  return ""
}

$pfx = Get-IniVal "codesign" "pfx_path"
$pwd = Get-IniVal "codesign" "pfx_password"
$ts = Get-IniVal "codesign" "timestamp_url"
$st = Get-IniVal "codesign" "signtool"
if (-not $ts) { $ts = "http://timestamp.digicert.com" }
if (-not $pfx -or -not (Test-Path -LiteralPath $pfx)) {
  throw "codesign.pfx_path 无效: $pfx"
}
if (-not $pwd) { throw "codesign.pfx_password 为空" }

if (-not $st) {
  $cands = @(
    "${env:ProgramFiles(x86)}\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe",
    "${env:ProgramFiles(x86)}\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe"
  )
  foreach ($c in $cands) { if (Test-Path $c) { $st = $c; break } }
  if (-not $st) {
    $hit = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
      Select-Object -First 1
    if ($hit) { $st = $hit.FullName }
  }
}
if (-not $st -or -not (Test-Path $st)) { throw "未找到 signtool.exe（需安装 Windows SDK）" }

foreach ($f in $Files) {
  if (-not (Test-Path -LiteralPath $f)) { throw "文件不存在: $f" }
  Write-Host "Signing $f ..."
  & $st sign /fd SHA256 /f $pfx /p $pwd /tr $ts /td SHA256 $f
  if ($LASTEXITCODE -ne 0) { throw "signtool 失败: $f" }
  & $st verify /pa $f
  if ($LASTEXITCODE -ne 0) { throw "验签失败: $f" }
}
Write-Host "Sign OK"
