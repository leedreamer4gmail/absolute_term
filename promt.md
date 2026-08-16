# absolute_term 项目逻辑（最新版）

用自然语言描述完整构造。本文件永远是「当前最终形态」；别的 AI 按此应能复原项目。不要当变更日志往后面粘。

对外产品名：**小李的电商扫描器**。仓库目录、nginx 路径、服务名仍是 `absolute_term`（勿改 URL）。

---

## 目标

本机完成淘宝抓取 + OCR + 词表/LLM；用户扫描结果写入自己的 `shop_tb`/`goods_tb`。

扫描对象：淘宝/天猫商品广告文案、主图/详情图 OCR 文字。对照极限词与错误描述词表，并可选用 LLM（DeepSeek Flash）二次判定，找出疑似违规商品。

---

## 分工原则

- **Windows 本机客户端**：抓淘宝、本机 RapidOCR、**读本地词表**、LLM 调用（密钥只从云端临时下发到内存）、结果写入用户自己的库；本地另存店铺全文 md。
- **云端**：登录鉴权、LLM 密钥托管、PostgreSQL 存用户店铺/问题商品、网页查看结果。云端**不负责**日常打淘宝（服务器 IP 易被风控）。服务器上可有同名词表文件，仅备用/服务器扫描用，**不是**本机扫描词表来源。

---

## Qoder 接手

给下一个 AI 的操作手册。产品逻辑仍看本文其它章节；本章只写「怎么干活、别踩坑」。

### 必须遵守

- 简体中文。`fix.md` **只读**，改了视为没做完。
- 数据不对就报错，禁止猜、禁止静默填空。
- 硬编码参数进 `config.ini`，后台设置白名单可改，备注写清楚。
- 网页弹窗复用 `www/js/dialog.js`（AppDialog）；UI 能一行就一行。
- 逻辑变了就**覆盖** `promt.md` 对应段落，不要往后面粘变更日志。新函数写进 `tree.md`。
- 未要求不要 git commit / push。改了 `api_server.py` / `db.py` / 云端 `config.ini` 必须 `sudo systemctl restart absolute`。
- 本仓交付物只叫 `absolute_term_Setup.exe` / `absolute_term_update_Setup.exe`。**不要**套用梦想直播的 mengxiangzhibo 包名。

### 仓库与云端

- 仓库：本机开发目录（Windows）。对外名 **小李的电商扫描器**；URL/服务名仍是 `absolute_term`，勿改。
- 公众页：`https://leedreamer.cn/absolute_term/`
- 生产 SSH / 绝对路径 / 数据库连接：**只写服务器本机笔记或 `config.ini.local`，禁止写进公开 Git 仓库。** systemd 服务名 `absolute`，Unix socket 示例 `/run/absolute/absolute.sock`。
- 库：服务器本机 PostgreSQL（库名 `absolute_term_db`）。本机开发机通常没有这套库。
- 密钥只在服务器 `config.ini.local`（db 密码、password_salt、llm api_key、短信 AK/SK）。**禁止**写进回复、禁止提交。
- 公司内部另一套 `/absolute/` 与本仓库无关，勿混。
- 本仓没有 OSS 配置。安装包上传用现有 `packaging\build_installer.ps1 -Publish`（目标主机用环境变量或本地配置，勿把服务器 IP/账号写进公开脚本默认值）。改完云端 `config.ini` 的 `client_app_version` 再重启服务，顺序：先传 exe，再抬版本号，避免用户下到旧包。

### 打包（新用户要能开箱跑）

1. 三处同号同抬：`client/version.py` = `packaging/absolute.iss` 的 `MyAppVersion` = 云端 `[client_release] client_app_version`
2. `packaging\build_onedir.ps1` → `packaging\build_installer.ps1 -Publish`
3. spec **必须**硬拷 RapidOCR 三个 `.onnx` + `config.yaml`，`collect_all("cv2")`，**禁止** `excludes` 掉 `cv2`。Playwright 驱动打进包。Chrome **不**打进包，用户本机自装。
4. Inno 给 `{app}\file` 普通用户写权限（`Permissions: users-modify`）。源文件/Excel 默认 `{app}\file\<用户名>\shops|output`，不是 AppData。装进 Program Files 若没这权限，新用户登录后建目录会直接报错（这是对的，不要再偷偷改去 C 盘）。
5. 打包脚本缺 onnx / cv2 / playwright 必须失败，不要出残包。

### 客观限制（不要装懂、不要空转）

- **淘宝抓取无法代测**：要真 Cookie、会碰到滑块。改代码后让用户本机验证。滑块没有「彻底解决」。
- **fix.md 若写「图1～图4」**：以用户消息里的截图为准；没图就先要复现步骤，不要猜 UI。
- **云端库不在本地**：改 db/API 用 SSH 部署并重启 `absolute`。不要假装本地 Postgres 能连。
- **Tk 窗口看不到、点不到**：UI 修完靠用户反馈。双击/勾选/路径以代码状态机为准，不要用「应该没问题」搪塞。

### 建议

- 先读 `promt.md` 全文 + `tree.md` + 当前 `fix.md`，再改代码。
- 客户端入口 `client/app.py`；路径逻辑 `client/shop_store.py`；OCR `scanner.py` + `client/local_scan.py`。
- 新用户路径：无 token 自动弹登录 → 登录后切到 `file/<用户名>/` → 词表从包内复制 → OCR 模型在 `_internal`。缺 Chrome 只影响抓取，应明确报「未找到 Google Chrome」，不要改开 Edge。
- 测试代码只放 `test/`，用完删除。

---

## LLM

- 服务器：`deepseek-ai/DeepSeek-V4-Flash`（SiliconFlow；key 在 `config.ini.local` 的 `[llm] api_key`）
- 判定提示词：`config.ini [llm] judge_prompt`（占位 `{title}` `{url}` `{hits_block}` `{rules}`）；管理员网页可改
- 提示词须区分「绝对化推销」与「客观规格/数量/时间」（如包装最小规格、最大承重、最多可优惠 ≠ 违规）
- 词表先命中 → LLM 二次判定；结构化 JSON 里 `violate:false` 的命中**不入库**；网页/Excel 展示也只保留 `violate:true`
- 客户端：每次 `GET /client/bundle` 取到内存（含 judge_prompt），不落盘；禁止写 `llm.ini` / 带密钥的 bundle 文件

---

## 客户端抓取

- 入口：`python client\app.py`（或安装包）；版本 `client/version.py`；窗口标题 `小李的电商扫描器 v…`（`APP_TITLE`）
- 窗口图标：`file/img/logox.jpg`；版面不放大 Logo；UI 暗色系、按钮偏小
- 布局：左栏上=链接或自动面板 / 条数/Cookie + **absolute|wrong 词表左右并排**（默认可见行高）+ 按钮；左栏下=日志；右栏=已扫店铺。**上栏默认约占六成**（`left_sash≈480`），禁止日志地包天
- **竖向柱子**：`LEFT_SASH_MIN=360`；启动须等窗体映射完成再还原 sash（未就绪禁止 clamp/写 ini，否则会把柱子夹成最小值导致「打开全封闭」）
- **布局持久化**：本机账号 ini `[layout]`（`layout_version` 当前 **4**；安装包写用户目录，源码写 `client/config.ini`）；拖柱结束后才写 sash；Cookie/词表行高可持久化
- Cookie 底边可拖改行高；检查更新弹进度窗（不刷日志）
- Cookie 标签下「如何获取cookies」：打开 `config.ini [ui] cookie_guide_url`（默认 `…/guide.html`，**纯静态免登录**）
- 网页下载客户端按钮前带下载小图标
- 问题商品窗：对齐云端三列表（商品名 | 命中词红色 | 摘要可多行）；底栏「下载 Excel」「关闭」
- 日志区只显示抓取情况，不刷 `absolute_fetcher v… → api` 这类细节
- 菜单「用户」：未登录 →「登录…」「注册（网页）」；已登录 → `用户名 · 余额 x`（点击打开本站充值页。客户端先 `POST /auth/web-ticket` 拿一次性票，用 `?ticket=` 打开网页，网页 `POST /auth/consume-ticket` 换**网页**会话并写入 localStorage，**不必再填用户名密码**。票 TTL 见 `[auth] web_ticket_ttl_seconds`，默认 120 秒、用一次即废。充值 URL 来自 `config.ini [ui] recharge_url`）、退出、注册（网页）、**提意见…**（单例窗：一行说明+文本框+提交 → `POST /suggestion` 进 suggestion_tb，未登录先报错）、刷新店铺、检查更新。注册打开 `[ui] register_url`。客户端先问云端 `GET /ui`，再读安装包 `_internal/config.ini`。**新用户启动无 token 时自动弹出登录窗**
- **客户端登录 ≠ 网页会话**：`POST /login` 带 `client:true`，云端发 `session_tb.channel=client` 的 Bearer token，**不过期**，只在「退出登录」或改密时作废；落库，服务重启不丢。网页登录/注册/票据换会话走 `channel=web`，TTL 见 `[auth] session_ttl_seconds`。两边 token 互不影响。客户端若仍收到「未登录或会话已过期」（旧内存会话残留等），用本地记住的账号密码**静默重登一次**再重试，不弹过期吓用户
- **自动检查更新**：启动静默请求 `GET /client/app-update`；有新版本**先弹对话框**问「是否现在升级」（选是立即下载安装，选否保留菜单栏入口），同时菜单栏出现「有新版本 x.y.z」（一点即下载安装），用户菜单「检查更新」同步改文案；无新版本不占菜单栏、不弹窗
- 菜单「设置」：Excel 输出目录 + 店铺详情路径 + **每次获取店铺数**（本机账号 ini `[fetch] harvest_count`）；另有「打开抓取 Chrome」
- 「抓取条数」「分析条数」：0=全部
- **文本右键菜单**：`client/text_menu.py` 给 Entry/Text 绑剪切/复制/粘贴/删除/全选（日志与源文件正文只读则仅复制+全选）；Ctrl+A 全选
- 已扫店铺：
  - 行右侧三个图标按钮（悬停出文字）：看源文件 / 生成 Excel / 打开 Excel（无 xlsx 则打开按钮禁用）
  - 看源文件：本地有 md 就读本地；没有则 `GET /shops/source` 从 `shop_tb.source_md` 下载再打开。左列表「全部商品」+ 各商品标题（过长显示…，拖中间柱子改宽）；点标题右侧只显示该商品块；右栏搜索只在**当前显示文字**里高亮，并显示 **共 N 字 · 第 i/m 处**；↑/↓（或 F3 / Shift+F3 / Enter）跳转命中；Markdown 源码着色不做 preview
  - **右键菜单「删除此店铺」**：`POST /shops/delete`（`tb_shop_id`），硬删 `shop_tb`（`goods_tb` CASCADE）；本地 md 不删
  - **标题旁总复选框**：勾上全选、再点取消全选；每行复选框始终可点。`shops_for_action`：**勾选的店优先**，没有勾选才用点选高亮行。勾选或点选后「重新扫描 / 分析店铺」都能跑，禁止只认高亮不认勾选
  - **点选已扫店铺**：把该店活着的种子商品链接（`shop_link` 含 `id=`，否则 `item_ids[0]`）填进「商品链接」输入框
  - **双击已扫店铺**：打开问题商品窗。已经打开的（含最小化）只前置，不新建第二扇。源文件窗 / 登录 / 设置 / 待检列表同样单例。云端拉取未完成时连点双击不排队开窗
  - **重新扫描**：优先 `tb_shop_id`+`seller_id` 拉全店；缺任一则用种子链接再 `resolve_targets(url=)`，禁止因缺 ID 直接报死
- **目录分工**：账号 ini / Cookie / Chrome 配置仍在可写用户目录（安装包：`%LOCALAPPDATA%\小李的电商扫描器\`；源码：`client/`）。**Excel 与店铺源文件**默认在程序自己的 `file/<用户名>/output` 与 `file/<用户名>/shops`（源码即仓库 `file/leedreamer2/…`）。换账号只读自己的目录，不扫别人的 md，也不再默认写 AppData。设置里留空=跟账号走；手选绝对路径才写死。装进 Program Files 写不进去就报错，不偷偷改去 C 盘
- 本地店铺 md：扫描/重扫当下这一家会把全文写入云端 `shop_tb.source_md`。**启动和刷新店铺列表不做全量对账**。用户点「看源文件」或「分析店铺」时才 `ensure_local_shop_md`：本地有 md 就用本地；没有才 `GET /shops/source`。两边都没有则明确报错
- Excel：默认 `file/<用户名>/output`，以店铺名命名。无文件时「打开 Excel」图标禁用
- **OCR**：本机 RapidOCR。下载图必须带淘宝 Cookie + Referer，先 PIL 转 JPEG 再识别（webp 也能跑）。有图 URL 但下载/识别失败时，md 写「主图/详情图 OCR 失败: 原因」，禁止再静默写成「无主图 OCR」装没扫过。无图 URL 才写「无主图 OCR / 无详情文本」。分析时忽略失败占位句，避免拿报错当文案去配词
- 出错日志：`%LOCALAPPDATA%\小李的电商扫描器\client.log`（启动时日志区打印路径）。滑块不算错误；抓到标题「登录」视为未登录商品页，不写入 md
- 打包：`packaging/build_onedir.ps1` → `build_installer.ps1 -Publish`；产出单文件 Setup.exe。**必须**把 RapidOCR 三个 `.onnx` + `config.yaml`、OpenCV(`cv2`)、`pyclipper`/`shapely`、Playwright 驱动打进 `_internal`（spec 按目录硬拷模型，禁止 `excludes` 掉 `cv2`）。Chrome 本机自装，不打进包。版本三处同号：`client/version.py` = Inno `MyAppVersion` = `[client_release] client_app_version`
- Cookie / 两步流水线 / 词表可编辑：同前
- **滑块（结论：不能「彻底」解决）**：淘宝/阿里系拖动验证。现有 `auto_slider=1` 自动拖；失败等人。不存在一劳永逸补丁。
- **单店 vs 自动模式（两套版面）**：
  - **单店模式**（默认）：商品链接框暗字「填入要检查的店铺任意商品链接」（点入消失、离开空则显示）；只粘贴链接 +「开始抓取」。**不做**采链/待扫表/全自动。
  - 顶栏第一层「模式」下拉：单店模式 / 自动模式。选自动且 `user_tb.auto_scan=false` 时提示扣费 `auto_scan_fee_yuan` 元永久打开（同意则 `POST /auto-scan/enable`；余额不足提示充值）。已开通则立刻切版面（不在 UI 线程打 `/me`、`/scan-shops`）。
  - **全自动/半自动单选钮**：暗色样式（`TRadiobutton`/`TCheckbutton` 背景吃进 UI 底色，指示器用 input_bg，选中点 primary），**没有白底色方块**。
  - **全自动**：点「开始扫描」按 `scan_shop_tb` 逐家扫；列表空了按设置数量继续采新店，直到点停止或 `auto_random_max_shops`。**全自动下「获取数量/获取店铺」置灰不可点**（那是半自动专用；`apply_auto_kind` 按单选切换置灰/恢复）。
  - **半自动**：填获取数量 +「获取店铺」写入云端；点「开始扫描」只扫当前列表，扫完即停，不自动采新店。
  - 「查看待检店铺」：新窗口 excel 风格列出待检查店铺，可「下载 Excel」。不再用可点击 label / 双击开扫（与「开始扫描」重复）。
  - 主界面显示「正在扫：店名」。
- **采链 / 待检查库**：
  1. `client/link_harvest.py`：专用 Chrome 打开 `[client] link_harvest_url`（不够再搜 `link_harvest_keyword`，逗号分隔则每次随机抽一个词），`ChromeFetcher.collect_item_ids` 抽 `item.htm?id=`。对每个 id `resolve_shop_info`。
  2. 去重：本用户 `shop_tb` ∪ `scan_shop_tb`（`tb_shop_id`）出现过则跳过；没出现过 `POST /scan-shops/add` 写入云端。本地 `link_queue.json` 不再是自动模式主队列。
  3. **扫描模块不变**：mtop → Chrome CDP → OCR → 词表/LLM。`fetch_and_scan_one` / `analyze_shop_sync` 同一工作线程。
  4. 参数：云端 `GET /ui` 下发 `link_harvest_*` / `auto_random_*`（管理员网页可改）；读不到再退回安装包 `_internal/config.ini [client]`。客户端「每次获取店铺数」覆盖一次入队数量。
- **未做**：已扫店与本地 md/云端 goods 的 **商品 id 差集增量**；旺铺页 CDP 翻页扒全店。滑块仍可能要人拖。不接 Midscene / Playwright MCP / Computer Use 当主链路。**拼多多扫描未做**：方案已调研——首选拼多多开放平台商家自营 API（OAuth 授权自己店铺，pdd.goods.list.get/detail 拿全店再走现有 OCR+词表+LLM），不走爬虫逆向（风控极严、违法风险）

抓取链路简述：种子可以是用户粘贴的商品链接（须含 `id=`），或云端 `scan_shop_tb.shop_link` → mtop / Chrome CDP 抓详情 → ①扫描写 md → ②分析入库；成功则硬删待检查行。**上传过滤**：`upload_scan_results` 只上传 problem 非空的商品进 goods_tb；本轮扫过的全部 id/标题/总数放 `shop.item_ids`/`shop.item_titles`/`shop.goods_sum` 上报（云端据此算总数、清「改好了」的历史问题行）。

---

## 词表

- **本机扫描用**：源码读仓库 `file/absolute_words.md`、`file/wrong_word.md`；安装版首次从包内复制到 `%LOCALAPPDATA%\小李的电商扫描器\file\`，之后只读写用户副本。空格或换行分词。界面保存即生效。
- **服务器同名文件**：仅备用/服务器侧扫描；客户端扫描**不拉**云端词表。

---

## 云端

- 网页未登录：登录 / 注册 / 重置密码 / 找回用户名。登录页下方声明：本系统旨在帮助电商卖家自查店铺违规，规避风险，防止打架人恶意举报投诉使用，不得用于非法用途。注册手机号可不填、不发短信。重置密码：用户名 + 自己记得的手机 + 验证码即可，**不必事先绑定**；成功后把该手机记到账号上。找回用户名：列出曾用该手机重置/登记过的账号
- 网页提供「下载本机客户端」：登录页 + 主页按钮（前有下载图标），链接来自公开接口 `GET /client/app-update`（`main_url` / 版本号）；站点标题/顶栏为「小李的电商扫描器」
- **版本管理**：`config.ini [client_release]` 的 `client_app_version` / `download_main_url` / `download_update_url` / `download_main_label`；客户端启动自动查更新并在菜单栏提示；网页展示当前版本
- 安装包交付：`www/downloads/absolute_term_Setup.exe`（Inno 单文件，非源码 zip）
- **菜单页卡「使用方法」**：顶栏按钮跳转独立页 `www/guide.html`（**无需登录**）；登录页也有入口；客户端「如何获取cookies」直达同一 URL。步骤：Chrome → 产品页 → F12 → Network/Doc → F5 → `item.htm` → Copy as cURL (cmd) → 粘贴客户端 Cookie 框。配图 `www/img/cookie_guide.png`
- 下载客户端按钮前有下载图标
- **主页↔设置不抖**：登录区与主区分离；主区 `main-stack` grid 叠层；登录后预载设置 HTML；`lockMainStackHeight`；`html{overflow-y:scroll}` 防滚动条槽跳动
- **用户下拉菜单**：顶栏账号区只显示「👤 用户名 ▾」按钮，点开下拉归纳：用户名（旁 ✏️，点击**原地行内编辑**→确认调 `POST /auth/rename`，不弹对话框；同名不改算成功，重名/格式错行内红字提示）、余额+「充值」链接、手机（已填显示号码、点号码可改，未填给「设置手机用以找回密码」链接）、自动扫描（已开通只显示文字；未开通给「[开通]」链接弹确认框，费用按配置显示）、扫商品单价。点外面关闭。两个下拉（用户/管理）统一约 200px 宽不过宽，打开时左边对齐各自触发按钮（👤/⚙）的左边，窗口缩放实时重对齐
- **充值**：`POST /pay/recharge {amount_cents, channel: wechat|alipay}`（0.1～100000 元）。微信返回 `code_url`（前端用 `GET /pay/qr.png?data=` 渲染二维码）轮询 `GET /pay/orders/{no}`；支付宝返回 `pay_url` 直接跳转。回调 `POST /pay/wechat/notify` / `/pay/alipay/notify` 验签后 `mark_pay_order_paid` **幂等入账**（仅 pending→paid 加余额一次）。`[pay] enabled=0` 时充值入口明确报错。网关 `payments.py`/`pay_config.py`，商户密钥只读 `config.ini.local`（与 livestream-api 共享 secrets 目录）
- **启动页签参数**：带 `?tab=register|reset|recover` 打开时强制显示登录卡并切到对应页签，不被已保存会话覆盖成主页（客户端「注册」菜单用 `register_url`（含 `?tab=register`）直达注册页，而非已登录的主页）
- **菜单弹窗再点关闭**：充值 / 手机号 / LLM提示词 / 店铺问题列表均为单例；再点同一入口关闭；设置页再点回主页
- 弹窗一律 `AppDialog`（`www/js/dialog.js` → `/shared/ui.js` UiWindow）
- 用户库：`POST /scan/results` → `shop_tb` / `goods_tb`，并写入 `shop_tb.source_md`（店铺源文件全文）。允许只带 `source_md` 更新已有店
- `shop_uniq_tb`：平台总表保留；不由客户端自动灌
- 登录后直接出店铺列表；点店铺名开关问题商品弹窗（商品名/命中词/摘要）
- 顶栏只留 主页 / 待检查店铺 / 使用方法 / 充值 / 退出。管理员专属（设置 / 数据库 / LLM判定提示词 / 意见库）收进「⚙ 管理 ▾」下拉菜单，仅管理员显示
- **意见库**：管理员下拉打开 AppDialog，表格列 时间/用户/意见/删除按钮（人工删）；顶置「LLM 筛选有用建议」按钮 → `POST /suggestions/filter`，删完刷新并提示删了几条
- **管理员锚定**：`[auth] admin_user_id`（user_tb.id）是唯一管理员判据，**不认用户名**；`/me` 的 is_admin、`/admin/*`、LLM 提示词都只认这个 id；留空=没有管理员；改账号名不影响管理员身份
- **数据库页** `www/db.html`：仅管理员，新开页只读浏览白名单表（长文本截断，密码/Cookie 打码）。URL 来自 `config.ini [ui] db_admin_url`，默认 `{public_base}/db.html`
- **待检查店铺**：excel 紧凑表，列 `scan_shop_tb` 的店名+链接
- **自动扫描开通**：独立的「自动检查」页已删除。用户下拉「自动扫描」行：已开通只显示「已开通」；未开通显示「未开通 [开通]」，点「[开通]」弹 AppDialog 确认框，费用按 `[billing] auto_scan_fee_yuan` 配置显示（不写死）；确定则 `POST /auto-scan/enable` 扣款并把 `user_tb.auto_scan=true`；余额不足报错后引导充值
- 管理员设置：扫商品单价 + **自动扫描费用**（`[billing] auto_scan_fee_yuan`）+ **LLM 意见筛选提示词**（多行编辑框，读写 `/llm/filter-prompt`）
- 余额 / 充值 / 手机 / 自动扫描：收进用户下拉菜单；充值走 `/pay/*`（见上）
- **登录留痕**：登录/注册/重置/票据换会话成功即把最近登录 IP+城市写 `user_tb`（`last_login_*`）；管理员「数据库」页可直接看到
- LLM key 只在服务器 `config.ini.local`

公众入口：`https://leedreamer.cn/absolute_term/`  
公司内部另一套 `/absolute/`（独立目录与 systemd）与本仓库无关，**勿混**。

---

## 运行拓扑

```
用户 Windows
  python client/app.py
    → 抓取淘宝（mtop / Chrome CDP）
    → 本机 RapidOCR
    → 读 file/absolute_words.md + file/wrong_word.md
    → GET /client/bundle 只取 LLM（内存）与 OCR 张数等配置
    → 扫描 → POST /scan/results 入库
    → 本地店铺 md（`file/<用户名>/shops/<店名>.md`，去重追加；安装包目录 `{app}\file\<用户名>\shops`）

云端 Linux
  nginx → /absolute_term/     → www/index.html
       → /absolute_term/api/  → Unix socket → absolute.service → api_server.py
  代码目录：服务器上的项目根目录（勿把真实主机路径写进公开仓库）
  服务：absolute.service（socket 默认 /run/absolute/absolute.sock）
  PostgreSQL：absolute_term_db
  配置：config.ini + config.ini.local（db 密码、password_salt、llm api_key、阿里云短信 AK/SK）
```

---

## 数据库

库名：`absolute_term_db`。服务启动时 `init_schema()` 执行 `schema.sql` 并做列迁移。可选测试用户：仅当 `[auth] bootstrap_username` 与 `bootstrap_password` 都非空才创建/确保（口令写服务器 `config.ini.local`，开源仓库留空）。管理员**不**由用户名决定，由 `[auth] admin_user_id`（user_tb.id；留空=无管理员）锚定。密码哈希：`sha256(salt:password)`，盐在 `[db] password_salt`（放 local）。

- user_tb
  - id 主键
  - username 唯一用户名
  - password_hash 密码哈希
  - cookie 服务器侧备用淘宝 Cookie（本机抓取仍优先本地）
  - enable 是否启用（T/F）
  - balance 用户余额（numeric，默认 0）
  - auto_scan 是否已永久开通自动扫描（boolean，默认 false）
  - phone 手机号（可选，仅找回密码/用户名；空或 11 位 1[3-9]…）
  - last_login_ip / last_login_city / last_login_at 最近一次登录的 IP、归属城市、时间（每次登录成功自动覆盖；IP 取 X-Forwarded-For 首个/X-Real-IP，拿不到就不写；城市按 `[geo]` 接口查，查不到留空）
  - created_at / updated_at
- session_tb（Bearer 登录会话，落库；服务重启不丢）
  - token 主键（Authorization: Bearer …）
  - user_id 外键 → user_tb
  - channel：`client`=桌面客户端（expires_at 为空=不过期，只退出/改密删）；`web`=网页（expires_at 按 session_ttl）
  - expires_at / created_at / last_seen_at
- shop_tb（用户「我的店铺库」，按 user 隔离）
  - id 主键
  - user_id 外键 → user_tb
  - shop_name 店铺名
  - shop_link 店铺/入口链接（「进入店铺」用）
  - goods_sum 总商品数
  - bad_goods_sum 问题商品数
  - tb_shop_id 淘宝店铺 id（抓取用；与 user_id 唯一）
  - seller_id 卖家 id
  - item_ids / item_titles jsonb 缓存
  - status / last_error
  - source_md 客户端店铺源文件全文（换机下载、重扫覆盖）
  - created_at / updated_at
- scan_shop_tb（待检查队列；扫完**硬删除**，不软删）
  - id 主键
  - user_id 外键 → user_tb
  - shop_name 店铺名
  - shop_link 入口商品链接（扫描种子）
  - tb_shop_id 淘宝 shopId（与 shop_tb 去重；本用户 unique）
  - seller_id 卖家 id
  - item_id 种子商品 id
  - created_at
- sms_code_tb（短信验证码；校验成功后硬删除）
  - id 主键
  - phone / purpose（reset|recover|bind）/ code
  - expires_at / created_at
- goods_tb（店铺下**有问题**商品明细；屁事儿没有的不入库，防数据爆炸）
  - id 主键
  - shop_id 外键 → shop_tb
  - goods_name 商品名
  - goods_link 商品链接
  - problem 问题说明（md：主图/详情命中 + 可选 LLM JSON）；**非空才写入**，计入 bad_goods_sum
  - tb_item_id 淘宝商品 id（与 shop_id 唯一）
  - created_at / updated_at
  - 规则：客户端只上传 problem 非空的商品；服务端 problem 为空直接拒收报错。重扫时本轮扫过且没问题的商品 id 会把该店历史问题行硬删（改好了就出表）。shop_tb.goods_sum（总商品数）由客户端按本轮实扫数上报（`shop.goods_sum`/`item_ids`），**不再**按 goods_tb 行数反推。服务启动时 `cleanup_goods_tb` 清掉历史空 problem 行
- pay_order_tb（充值订单）
  - id 主键
  - user_id 外键 → user_tb
  - order_no 商户订单号（唯一）
  - channel 支付渠道（wechat|alipay）
  - amount_cents 金额（分）
  - status pending / paid / closed
  - trade_no 渠道交易号；paid_at 支付时间
  - created_at / updated_at
  - 规则：`mark_pay_order_paid` 仅当 status=pending 时置 paid 并加余额，重复回调不重复入账
- suggestion_tb（用户意见/建议）
  - id 主键
  - comment 意见正文（非空、≤2000 字，否则报错）
  - user_id 外键 → user_tb（谁提的）
  - date 提交时间（default now()）
  - 规则：客户端「提意见」写入；管理员「意见库」查看，可单条人工删，也可一键 LLM 筛选（毫无意义的直接删，拿不准的保留）
- shop_uniq_tb（平台总表，预留）
  - id 主键
  - shop_name 店铺名
  - shop_link 店铺链接（非空时唯一）
  - shop_content 店铺全文 md
  - content_hash shop_content 的 sha256，相同则不必更新
  - created_at / updated_at
  - 说明：客户端不自动上传；以后后台从用户库提取汇聚

---

## 主要 API（均在 /absolute_term/api 下，需登录的带 Bearer token）

- `POST /login` `{username,password,client?}` → token。`client:true`（桌面端）→ `session_tb.channel=client`，**不过期**；省略/false（网页）→ `channel=web`，TTL=`session_ttl_seconds`。会话落库，重启不丢。`POST /logout` 只删当前 token；`GET /me`…。登录/注册/重置密码/票据换会话成功后，自动记录该用户最近一次登录 IP 与归属城市到 `user_tb`（后台线程查 `[geo]` 接口，失败只留空不挡登录）。改密会作废该用户全部会话
- `POST /auth/web-ticket`（需登录）→ `{ticket, ttl}` 一次性网页登录票；`POST /auth/consume-ticket` `{ticket}` → 新 **web** token+user（免登录，用一次即废；与客户端 token 分离）
- `POST /auth/register` `{username, password, phone?}` → 用户名唯一、密码≥6、手机可选；成功即登录返回 token+user。用户名已存在报「用户名已存在」，不是 404
- `POST /auth/send-sms` `{purpose: reset|recover|bind, username?, phone}`：注册不发短信。reset 只要求用户名存在，手机不必事先绑定；recover 查已记下该手机的账号；bind 须已登录。无阿里云配置则明确报错，不假装发出。验证码一次性，重发间隔见 `[auth] sms_resend_seconds`
- `POST /auth/reset-password` `{username, phone, code, new_password}` → 校验后改密、记下该手机、并登录。不要求旧绑定一致
- `POST /auth/recover-usernames` `{phone, code}` → `{usernames, count}`
- `POST /auth/bind-phone`（需登录）`{phone, code}`
- `POST /auth/rename`（需登录）`{username}` → 改当前用户名。格式须 2～32 位字母/数字/下划线/中文；与他人重名报「用户名已被占用」；与现名相同视为成功不改。改名后原 token 继续有效
- `POST /pay/recharge`（需登录）`{amount_cents, channel}` → 建 pay_order_tb 订单并调支付网关；wechat 返 `code_url`/`order_no`，alipay 返 `pay_url`。`[pay] enabled=0` 报「充值未开启」；金额范围 0.1～100000 元，越界直接报错
- `GET /pay/orders/{order_no}`（需登录，仅本人）→ `{status}` 供前端轮询；本地仍 pending 时先向网关主动查单同步
- `GET /pay/qr.png?data=` → 把 code_url 渲染成二维码 PNG（qrcode 库）
- `POST /pay/wechat/notify` / `POST /pay/alipay/notify` → 渠道异步回调，验签后幂等入账（见 pay_order_tb 规则），返回渠道要求的应答
- `POST /suggestion`（需登录）`{comment}` → 写入 suggestion_tb；空内容/超 2000 字直接报错
- `GET /suggestions`（仅管理员）→ `{items:[{id,comment,user_id,date,username}]}`，新→旧
- `POST /suggestions/delete`（仅管理员）`{id}` → 人工删除单条；不存在报「意见不存在或已被删除」
- `POST /suggestions/filter`（仅管理员）→ 用服务端 LLM（[llm] 配置）逐条判定，把「毫无意义」的（乱码/纯占位/辱骂等）硬删，有价值的保留；只认 LLM 返回里真实存在的 id，拿不准的保留；返回 `{deleted, deleted_ids, items}`；LLM 没配置或解析失败直接报错，不猜。判定提示词读 `[llm] suggestion_filter_prompt`（未配置用内置默认）
- `GET|POST /llm/filter-prompt`（仅管理员）→ 读/写意见筛选提示词（`[llm] suggestion_filter_prompt`）；空内容报错，提示词里必须含 useless 输出格式要求否则报错。网页设置页「管理员 · LLM 意见筛选提示词」编辑框调它
- `GET /ui` → 免登录：recharge_url / register_url / cookie_guide_url / public_base / db_admin_url / link_harvest_* / auto_random_*
- `GET /client/bundle` → 本机用：LLM 配置（含 judge_prompt，内存）+ 扫描相关配置；**不含**本机词表内容作为扫描来源
- `POST /scan/results` → 写入 shop_tb / goods_tb；`shop.source_md` 覆盖店铺源文件；允许只传 source_md 更新已有店。**goods_tb 只收 problem 非空的商品**；客户端同时上报 `shop.item_ids`/`item_titles`/`goods_sum`（本轮全量），没问题的商品 id 会删掉该店历史问题行；goods_sum 只认上报值
- `GET /shops` → 当前用户店铺列表（含 `has_source_md`，不含全文）
- `GET /shops/source?shop_id=` → 当前用户某店 `source_md` 全文
- `POST /shops/delete` → `{shop_id}`（tb_shop_id）硬删已扫店铺
- `GET /scan-shops` → 当前用户待检查店铺
- `POST /scan-shops/add` → `{shops:[{shop_name,shop_link,tb_shop_id,seller_id,item_id}]}`；已在 shop_tb 或 scan_shop_tb 则跳过
- `POST /scan-shops/delete` → `{id}` 或 `{tb_shop_id}`，硬删
- `POST /auto-scan/enable` → 按 `auto_scan_fee_yuan` 扣费并置 auto_scan=true（已开通不重复扣）
- `POST /admin/scan-fee` `{yuan}`；`POST /admin/auto-scan-fee` `{yuan}`；`POST /admin/adjust`
- `GET /goods/list?shop_id=` → 店铺商品；返回 hit_keywords / hit_summary（摘要无 LLM 长理由）
- `GET|POST /llm/prompt` → 仅管理员 leedreamer 读写 `[llm] judge_prompt`
- `GET /admin/db/tables`、`GET /admin/db/rows?table=&limit=&offset=` → 仅管理员只读浏览白名单表
- `GET|POST /settings` → 可读可改的 config 白名单项（含短信 ttl/重发/位数、阿里云签名与模板；**不含** AK/SK）
- `GET|POST /files/limit`、`/files/wrong` → 服务器侧词表文件（备用）
- `POST /shop_uniq/upload` → 仅后台/手工
- 另有健康检查、Cookie、旧版 goods import、服务器扫描备用接口等；日常主路径是客户端入库 + 网页看库

---

## 关键文件（重建时按此落位）

- `api_server.py` — HTTP API（Unix socket）；注册/找回走 `/auth/*`
- `db.py` — 建表/鉴权/店铺与商品 CRUD / shop_uniq / 短信验证码 / 改用户名 / 充值订单
- `sms.py` — 阿里云 SendSms（urllib；密钥只读 local）
- `payments.py` / `pay_config.py` — 支付网关：微信 Native / 支付宝电脑网站（商户密钥只读 `config.ini.local`，与 livestream-api 共享 secrets 目录）；`[pay]` 段读取与开关
- `schema.sql` — 表结构
- `scanner.py` / `llm_config.py` — 词表匹配与 LLM 判定
- `fetch_item.py` / `fetch_shop.py` / `cookie_util.py` — 淘宝抓取与 Cookie
- `ocr_server.py` — 可选本地 OCR 服务
- `client/app.py` — 窗口客户端
- `client/local_scan.py` — 本机 OCR + 本地词表 + 内存 LLM
- `client/chrome_fetch.py` / `mouse_util.py` — CDP 与滑块；`collect_item_ids` 采链
- `client/link_queue.py` / `link_harvest.py` — 待扫表与自动采链
- `client/shop_store.py` — 本地店铺 md 去重
- `www/index.html` — 登录后店铺列表；用户下拉菜单（改名/充值/手机/自动扫描）；管理员下拉菜单（设置/数据库/LLM提示词/意见库）；自动扫描确认弹窗开通（无独立页）；点店名 AppDialog 弹窗
- `www/js/dialog.js` — AppDialog（封装 shared UiWindow）
- `file/absolute_words.md` / `file/wrong_word.md` / `file/shops/`
- `config.ini` + `config.ini.local` + `absolute.service`（含 `[llm] judge_prompt`）
- `promt.md` / `tree.md`

---

## 配置要点

- `config.ini`：非密钥（db 主机/库名、抓取间隔、OCR 张数、词表路径、LLM 的 api_url/model 等）；网页「设置」可改白名单项。`[auth] admin_user_id` 是管理员唯一锚点（user_tb.id；留空=没有管理员；**不在**设置面板白名单里，改管理员要直接改文件）。`[geo]` 段控制登录 IP 归属城市：`enabled`（1/0）、`api_url`（含 `{ip}` 占位，默认 ip-api.com 免费版）、`city_json_keys`（取城市字段顺序，默认 `city,regionName` 市查不到退回省）、`timeout_seconds`（超时留空不挡登录）。`[pay]` 段为充值开关与网关参数（`enabled` 等）；**商户密钥/证书路径只在 `config.ini.local`**，与 livestream-api 共享 secrets 目录。`[ui] register_url` 须带 `?tab=register` 保证客户端「注册」直达注册页
- `config.ini.local`：**勿提交** — db password、password_salt、llm api_key、阿里云短信 AK/SK、支付商户密钥/证书路径
- 本机账号 ini：安装版 `%LOCALAPPDATA%\小李的电商扫描器\config.ini`；源码 `client/config.ini`（可用 `client/config.example.ini` 作模板）

---

## 复原步骤摘要

1. 建 PostgreSQL 库 `absolute_term_db`，用户授权
2. 部署代码到服务器项目目录，写好 `config.ini.local`
3. 装依赖（见 `requirements.txt`），起 `absolute.service`，nginx 反代 `/absolute_term/` 与 `/absolute_term/api/`
4. Windows：装依赖 → 编辑本机 `file/` 词表 → `python client\app.py` → 登录云端 → 抓取扫描入库
5. 浏览器打开公众页登录，直接看已扫店铺与问题商品展开表
