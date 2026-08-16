#Requires -Version 5.1
<#
.SYNOPSIS
  Assemble payload + compile Setup.exe -> packaging\out\absolute_term\
  PyInstaller onedir + Inno Setup 7 (ISCC). Single-file installer (payload embedded).
#>
param(
  [switch]$Publish
)

$ErrorActionPreference = "Stop"
$Pack = $PSScriptRoot
$Root = Split-Path -Parent $Pack
$Dist = Join-Path $Root "dist\极限词扫描"
$Payload = Join-Path $Pack "payload"
$OutRoot = Join-Path $Pack "out"
$Bundle = Join-Path $OutRoot "absolute_term"

if (-not (Test-Path $Dist)) {
  throw "Missing $Dist . Run packaging\build_onedir.ps1 first."
}

$iscc = $null
foreach ($c in @(
  "ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
  "E:\Program Files\Inno Setup 7\ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)) {
  $cmd = Get-Command $c -ErrorAction SilentlyContinue
  if ($cmd) { $iscc = $cmd.Source; break }
  if (Test-Path $c) { $iscc = $c; break }
}
if (-not $iscc) {
  throw "ISCC.exe not found. Install Inno Setup 7."
}

# Strip accidental .py from dist before packaging
Get-ChildItem -LiteralPath $Dist -Recurse -Filter *.py -ErrorAction SilentlyContinue | Remove-Item -Force

if (Test-Path $Payload) { Remove-Item -LiteralPath $Payload -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Payload | Out-Null
Write-Host "robocopy dist -> packaging\payload ..."
robocopy $Dist $Payload /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy payload failed code=$LASTEXITCODE" }
if (-not (Test-Path (Join-Path $Payload "极限词扫描.exe"))) {
  throw "payload missing 极限词扫描.exe"
}

New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null
Push-Location $Pack
try {
  Write-Host "ISCC=$iscc"
  & $iscc (Join-Path $Pack "absolute.iss")
  if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
} finally {
  Pop-Location
}

$setupExe = Get-ChildItem -Path $OutRoot -Filter "Setup*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $setupExe) { throw "Setup*.exe not found under out\" }

if (Test-Path $Bundle) { Remove-Item -LiteralPath $Bundle -Recurse -Force }
New-Item -ItemType Directory -Path $Bundle | Out-Null
Copy-Item $setupExe.FullName (Join-Path $Bundle "Setup.exe") -Force

Write-Host "OK -> $($Bundle)\Setup.exe"

# 可选：若 config.ini.local [codesign] 有 pfx，则签名后再交付
$signScript = Join-Path $Pack "sign.ps1"
$localIni = Join-Path $Root "config.ini.local"
if ((Test-Path $signScript) -and (Test-Path $localIni) -and (Select-String -Path $localIni -Pattern '^\s*pfx_path\s*=' -Quiet)) {
  Write-Host "codesign: signing Setup.exe ..."
  & powershell -NoProfile -ExecutionPolicy Bypass -File $signScript -Files @((Join-Path $Bundle "Setup.exe"))
} else {
  Write-Host "codesign: skipped (no [codesign] pfx in config.ini.local). SmartScreen may warn until signed."
}

if ($Publish) {
  $remote = "admin@8.219.6.216"
  $remoteDir = "/home/project/absolute_term/www/downloads"
  Write-Host ("Upload to {0}:{1} ..." -f $remote, $remoteDir)
  ssh -o BatchMode=yes $remote "mkdir -p $remoteDir"
  $localSetup = Join-Path $Bundle "Setup.exe"
  scp -o BatchMode=yes $localSetup ($remote + ":" + $remoteDir + "/absolute_term_Setup.exe")
  scp -o BatchMode=yes $localSetup ($remote + ":" + $remoteDir + "/absolute_term_update_Setup.exe")
  ssh -o BatchMode=yes $remote "cd $remoteDir; rm -f absolute_term_portable.zip absolute_fetcher_win.zip absolute_fetcher.zip absolute_term.zip; ls -lh"
  Write-Host "Set server config.ini [client_release] download_*_url to downloads/*.exe"
}
