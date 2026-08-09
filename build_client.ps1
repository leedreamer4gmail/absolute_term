# 在 Windows 上打包 absolute_fetcher.exe 到 www\downloads\
# 需要: Python 3.10+、pip install pyinstaller requests beautifulsoup4
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Out = Join-Path $Root "www\downloads"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$Ver = (Select-String -Path (Join-Path $Root "client\version.py") -Pattern 'CLIENT_VERSION\s*=\s*"([^"]+)"').Matches.Groups[1].Value
Write-Host "Building absolute_fetcher.exe v$Ver"

python -m pip install -q pyinstaller requests beautifulsoup4 browser-cookie3
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name absolute_fetcher `
  --paths $Root `
  --hidden-import=bs4 --hidden-import=requests --hidden-import=cookie_util `
  --hidden-import=fetch_item --hidden-import=fetch_shop `
  --distpath (Join-Path $Root ".client_build\dist") `
  --workpath (Join-Path $Root ".client_build\work") `
  --specpath (Join-Path $Root ".client_build") `
  (Join-Path $Root "client\app.py")

Copy-Item -Force (Join-Path $Root ".client_build\dist\absolute_fetcher.exe") (Join-Path $Out "absolute_fetcher.exe")
Write-Host "OK -> $Out\absolute_fetcher.exe"
