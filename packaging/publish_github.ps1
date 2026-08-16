#Requires -Version 5.1
<#
.SYNOPSIS
  推送已提交的开源清理到 GitHub，并创建带安装包的 Release（v1.6.1）。

  先登录：
    & "$env:ProgramFiles\GitHub CLI\gh.exe" auth login

  再运行：
    powershell -NoProfile -ExecutionPolicy Bypass -File packaging\publish_github.ps1
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$gh = Join-Path $env:ProgramFiles "GitHub CLI\gh.exe"
if (-not (Test-Path $gh)) { throw "未找到 gh.exe，请先安装 GitHub CLI" }

& $gh auth status
if ($LASTEXITCODE -ne 0) { throw "请先: gh auth login" }

Write-Host "Push origin main ..."
git push -u origin HEAD
if ($LASTEXITCODE -ne 0) { throw "git push 失败" }

$setup = Join-Path $Root "packaging\out\absolute_term\Setup.exe"
if (-not (Test-Path $setup)) {
  throw "缺少 $setup 。先跑 packaging\build_onedir.ps1 与 build_installer.ps1"
}

$tag = "v1.6.1"
$notes = @"
## 小李的电商扫描器 $tag

- 完整安装：``absolute_term_Setup.exe``
- 升级安装：``absolute_term_update_Setup.exe``（与完整包同内容，便于「检查更新」）

更多工具：https://leedreamer.cn/

产品页：https://leedreamer.cn/absolute_term/
"@

# 复制两份文件名再上传
$tmp = Join-Path $env:TEMP "absolute_term_release_$tag"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
Copy-Item $setup (Join-Path $tmp "absolute_term_Setup.exe") -Force
Copy-Item $setup (Join-Path $tmp "absolute_term_update_Setup.exe") -Force

$existing = & $gh release view $tag 2>$null
if ($LASTEXITCODE -eq 0) {
  Write-Host "Release $tag 已存在，上传/覆盖附件 ..."
  & $gh release upload $tag `
    (Join-Path $tmp "absolute_term_Setup.exe") `
    (Join-Path $tmp "absolute_term_update_Setup.exe") `
    --clobber
} else {
  Write-Host "创建 Release $tag ..."
  & $gh release create $tag `
    (Join-Path $tmp "absolute_term_Setup.exe") `
    (Join-Path $tmp "absolute_term_update_Setup.exe") `
    --title "小李的电商扫描器 $tag" `
    --notes $notes
}

Write-Host "OK. 仓库设为 Public: https://github.com/leedreamer4gmail/absolute_term/settings"
