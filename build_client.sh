#!/usr/bin/env bash
# 打包可下载客户端到 www/downloads/
# - absolute_fetcher.zip：源码包
# - absolute_fetcher_win.zip：Windows 便携包（内嵌 embeddable Python）
# Windows 单文件 exe：在 Windows 上运行 build_client.ps1
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/www/downloads"
STAGE="$ROOT/.client_build"
VER="$(python3 -c "import pathlib,re; t=pathlib.Path('$ROOT/client/version.py').read_text(); print(re.search(r'CLIENT_VERSION\s*=\s*[\"\\']([^\"\\']+)', t).group(1))")"
mkdir -p "$OUT"
rm -rf "$STAGE"
mkdir -p "$STAGE"

echo "==> version $VER"

SRC_DIR="$STAGE/absolute_fetcher_$VER"
mkdir -p "$SRC_DIR/client"
cp -a "$ROOT/client/app.py" "$ROOT/client/version.py" "$ROOT/client/__init__.py" \
  "$ROOT/client/requirements.txt" "$SRC_DIR/client/"
cp -a "$ROOT/fetch_item.py" "$ROOT/fetch_shop.py" "$ROOT/cookie_util.py" "$SRC_DIR/"
# 写入默认上传地址，便于开箱
mkdir -p "$SRC_DIR/client/data"
cat > "$SRC_DIR/client/data/client.json" <<'EOF'
{
  "server": "https://leedreamer.cn/absolute/api"
}
EOF
cat > "$SRC_DIR/run.bat" <<'EOF'
@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 python，请安装 Python 3.10+ 并勾选 Add to PATH，或改用 absolute_fetcher_win.zip
  pause
  exit /b 1
)
python -m pip install -q -r client\requirements.txt
python client\app.py
if errorlevel 1 pause
EOF
cat > "$SRC_DIR/run.sh" <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 -m pip install -q -r client/requirements.txt
exec python3 client/app.py "$@"
EOF
chmod +x "$SRC_DIR/run.sh"
cat > "$SRC_DIR/README.txt" <<EOF
极限词本机抓取客户端 v$VER

【用途】
在您自己的电脑上抓淘宝/天猫商品详情，再上传到扫描网站。
抓取走本机网络，避开服务器机房 IP 被淘宝风控。

【推荐：Windows 便携包 absolute_fetcher_win.zip】
解压后双击「绝对词抓取客户端.bat」（首次会自动装依赖，需联网）。

【本源码包】
需已安装 Python 3.10+，双击 run.bat。

【流程】
1. 粘贴淘宝 Cookie（或读 Chrome）
2. 填商品链接 → 开始抓取 → 上传
3. 回网站点「开始扫描」

【注意】
Cookie 只保存在本机，默认不上传服务器。勿过快连打。
EOF

(cd "$STAGE" && zip -qr "$OUT/absolute_fetcher.zip" "absolute_fetcher_$VER")
cp -f "$SRC_DIR/README.txt" "$OUT/README.txt"

# Windows 便携：embeddable CPython（首次在用户机器上 get-pip）
PY_VER="3.12.7"
PY_ZIP="python-$PY_VER-embed-amd64.zip"
PY_URL="https://www.python.org/ftp/python/$PY_VER/$PY_ZIP"
WIN_NAME="absolute_fetcher_win_$VER"
WIN_DIR="$STAGE/$WIN_NAME"
mkdir -p "$WIN_DIR/python" "$WIN_DIR/app"
echo "==> downloading embeddable Python $PY_VER"
if curl -fsSL -o "$STAGE/$PY_ZIP" "$PY_URL"; then
  unzip -qo "$STAGE/$PY_ZIP" -d "$WIN_DIR/python"
  PTH=$(ls "$WIN_DIR/python"/python*._pth 2>/dev/null | head -1 || true)
  if [[ -n "${PTH:-}" && -f "$PTH" ]]; then
    if grep -q '^#import site' "$PTH"; then
      sed -i 's/^#import site/import site/' "$PTH"
    elif ! grep -q '^import site' "$PTH"; then
      echo 'import site' >> "$PTH"
    fi
  fi
  curl -fsSL -o "$WIN_DIR/get-pip.py" https://bootstrap.pypa.io/get-pip.py
  cp -a "$SRC_DIR/." "$WIN_DIR/app/"
  cat > "$WIN_DIR/绝对词抓取客户端.bat" <<'EOF'
@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0python\python.exe"
if not exist "%PY%" (
  echo 缺少内嵌 Python
  pause
  exit /b 1
)
if not exist "%~dp0python\Lib\site-packages\requests" (
  echo 首次运行：正在安装依赖…
  "%PY%" "%~dp0get-pip.py" --no-warn-script-location
  "%PY%" -m pip install -q -r "%~dp0app\client\requirements.txt"
)
"%PY%" "%~dp0app\client\app.py"
if errorlevel 1 pause
EOF
  (cd "$STAGE" && zip -qr "$OUT/absolute_fetcher_win.zip" "$WIN_NAME")
  echo "OK win portable"
else
  echo "WARN: embeddable Python 下载失败，跳过 win 便携包"
fi

python3 - <<PY
import hashlib, json
from pathlib import Path
out = Path("$OUT")
manifest = {"version": "$VER", "files": []}
for p in sorted(out.iterdir()):
    if p.name.startswith('.') or not p.is_file():
        continue
    manifest["files"].append({
        "name": p.name,
        "size": p.stat().st_size,
        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
    })
(out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(manifest, ensure_ascii=False, indent=2))
PY
echo "DONE -> $OUT"
