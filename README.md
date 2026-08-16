# 小李的电商扫描器（absolute_term）

Windows 本机客户端 + 云端 API：淘宝/天猫店铺广告文案与主图/详情图 OCR，对照极限词与错误描述词表（可选 LLM），找出疑似违规商品。

对外产品名：**小李的电商扫描器**。仓库目录、nginx 路径、服务名仍是 `absolute_term`。

- 产品页：https://leedreamer.cn/absolute_term/
- 下载安装包：见本仓库 [Releases](../../releases)（完整包 `absolute_term_Setup.exe`，升级包 `absolute_term_update_Setup.exe`），或官网 downloads
- 更多实用工具：https://leedreamer.cn/

> 密钥只写服务器 `config.ini.local`（数据库密码、`password_salt`、LLM `api_key`、短信/支付商户密钥）。**不要**把口令、token、证书提交进 Git。

---

## leedreamer.cn 还有哪些工具

本仓库只是其中之一。主站 [leedreamer.cn](https://leedreamer.cn/) 上还有：

| 项目 | 简介 |
|------|------|
| **absolute_term**（本仓库） | 店铺合规扫描：本机抓取淘宝 + OCR + 词表/LLM，云端存结果与账号 |
| **futures** | 期货日内交易系统（Python · Flask · Trading） |
| **keng** | 踩坑笔记 + AI 秘书 Lucy（Python · FastAPI） |
| **EnglishWord** | 英语单词全栈 App：词典查询 + 进度追踪（React · tRPC） |
| **VideoDown** | 粘贴抖音/B站等链接，解析清晰度后下载到本机（Python · yt-dlp） |
| **codeExam** | 8 道考题衡量 AI 编程工具真实水平；支持网页浏览与下载 |
| **livestream** | 梦想直播 · 实景带货直播助手（含 livestream-api） |
| **company** | 公司内网工具集：财务秘书、客服、库存、销售、标签校验等（内部，不对外） |

想找其它工具、说明或下载入口，直接打开：**https://leedreamer.cn/**

---

## 功能概要

- **本机客户端**：专用 Chrome 登录淘宝、抓详情、RapidOCR、本地词表、可选 LLM；结果写入用户自己的云端店铺库
- **云端**：注册/登录、余额与充值、店铺与问题商品、网页查看、客户端自动更新
- **不负责**：在服务器上日常打淘宝（机房 IP 易被风控）

---

## 快速开始

### 下载安装（推荐）

1. 打开 [Releases](../../releases) 下载 `absolute_term_Setup.exe`
2. 安装后启动「小李的电商扫描器」
3. 注册/登录云端 → 本机 Chrome 登录淘宝 → 粘贴商品链接开始抓取

升级用同版本的 `absolute_term_update_Setup.exe`，或客户端内「检查更新」。

### 源码运行

```powershell
cd absolute_term
python -m pip install -r requirements.txt
python client\app.py
```

### 自建云端（可选）

```bash
# 配置 config.ini + config.ini.local 后
sudo systemctl restart absolute
```

公开仓库里的 `config.ini` **不含**数据库密码、盐、bootstrap 口令、管理员 id；这些在服务器 local 里配。

---

## 目录

| 路径 | 说明 |
|------|------|
| `client/` | Windows 客户端 |
| `api_server.py` | 云端 API |
| `db.py` / `schema.sql` | PostgreSQL |
| `www/` | 网页 |
| `packaging/` | PyInstaller + Inno 打包脚本 |
| `promt.md` / `tree.md` | 逻辑说明与文件树 |

打包：

```powershell
packaging\build_onedir.ps1
packaging\build_installer.ps1 -Publish
```

---

## 许可与声明

本系统旨在帮助电商卖家自查店铺广告表述、规避合规风险，请勿用于未授权抓取或其它违法用途。

公司内部旧入口 `/absolute/` 与本公众版仓库无关，请勿混用。
