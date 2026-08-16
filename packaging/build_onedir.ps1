#Requires -Version 5.1
<#
.SYNOPSIS
  打 absolute_term 客户端 onedir（参考 livestream packaging/build_onedir.ps1）。
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Pack = $PSScriptRoot
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  $venvPy = (Get-Command python -ErrorAction Stop).Source
  Write-Host "警告: 未找到 $Root\.venv ，使用 PATH python: $venvPy"
}

Push-Location $Root
try {
  & $venvPy -m pip show pyinstaller 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    & $venvPy -m pip install -U pyinstaller
  }
  Write-Host "PyInstaller onedir ..."
  & $venvPy -m PyInstaller --noconfirm --clean (Join-Path $Pack "absolute_onedir.spec")
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }
  $dist = Join-Path $Root "dist\极限词扫描"
  if (-not (Test-Path $dist)) { throw "缺少产出 $dist" }
  $needOnnx = @(
    "ch_PP-OCRv4_det_infer.onnx",
    "ch_PP-OCRv4_rec_infer.onnx",
    "ch_ppocr_mobile_v2.0_cls_infer.onnx"
  )
  $onnxNames = @(Get-ChildItem -LiteralPath $dist -Recurse -Filter *.onnx -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
  foreach ($n in $needOnnx) {
    if ($onnxNames -notcontains $n) { throw "payload 缺少 RapidOCR 模型 $n" }
  }
  $cv2ok = (Test-Path (Join-Path $dist "_internal\cv2")) -or @(Get-ChildItem -LiteralPath $dist -Recurse -Filter "cv2*.pyd" -ErrorAction SilentlyContinue)
  if (-not $cv2ok) { throw "payload 缺少 OpenCV (cv2)" }
  if (-not (Test-Path (Join-Path $dist "_internal\playwright"))) { throw "payload 缺少 playwright" }
  Write-Host "OK -> $dist （OCR onnx / cv2 / playwright 已打进包）"
  Write-Host "下一步: packaging\build_installer.ps1"
} finally {
  Pop-Location
}
