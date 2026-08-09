# 广告极限词扫描（absolute_term）

独立项目：淘宝/天猫广告极限词扫描 + **本机抓取客户端**（抗服务器 IP 风控）。

## 目录

- `client/` — 本机抓取客户端（源码，Windows 可直接跑）
- `api_server.py` — 网站 API（服务器）
- `www/` — 前端页面
- `fetch_item.py` / `fetch_shop.py` — 淘宝抓取
- `scanner.py` — 词表 + LLM 判定

## 本机开发（Windows）

### Clone（推荐：从服务器 SSH）

```powershell
mkdir D:\project -Force
cd D:\project
git clone ssh://admin@leedreamer.cn/home/project/repos/absolute_term.git absolute_term
cd absolute_term
```

（GitHub 独立仓库建好后也可：`git clone git@github.com:leedreamer4gmail/absolute_term.git`）

### 跑客户端（源码即可，安装包以后再做）

```powershell
python -m pip install -r requirements.txt
python client\app.py
```

流程：本机抓详情并上传 → 网页点「开始扫描」。

## 服务器

```bash
sudo cp absolute.service /etc/systemd/system/absolute.service
sudo systemctl daemon-reload
sudo systemctl restart absolute
```

配置见 `config.ini`（密钥用服务器本地 `config.ini.local` 覆盖复制，勿提交密钥）。

## 以后打包

Windows 上运行 `build_client.ps1` 打 exe；服务器可跑 `build_client.sh` 出便携 zip。
