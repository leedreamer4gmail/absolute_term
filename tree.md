# absolute_term 文件树

```
absolute_term/
├── schema.sql / db.py / api_server.py / sms.py / payments.py / pay_config.py
│   ├── user_tb.auto_scan / user_tb.phone / user_tb.last_login_ip|last_login_city|last_login_at；session_tb（client 不过期 / web 有 TTL，落库重启不丢）；scan_shop_tb 待检查队列；sms_code_tb；pay_order_tb 充值订单（order_no 唯一，status pending/paid/closed）；suggestion_tb 用户意见（comment/user_id/date）
│   ├── 登录成功自动记最近登录 IP+城市（X-Forwarded-For → [geo] 接口查归属地）
│   ├── POST /login client:true → 客户端不过期会话；网页走 TTL；POST /scan/results 只落「有问题」商品；goods_sum 由客户端上报，不按 goods_tb 行数猜
│   ├── GET /me → balance / phone / auto_scan / scan_fee_* / auto_scan_fee_*
│   ├── POST /auth/register|send-sms|reset-password|recover-usernames|bind-phone（重置不必先绑定手机）
│   ├── POST /auth/rename（改用户名；格式+唯一性校验，同名幂等）
│   ├── POST /pay/recharge（微信 Native / 支付宝电脑网站）；GET /pay/orders/{no}（状态轮询）；GET /pay/qr.png（code_url → PNG）；POST /pay/wechat/notify、/pay/alipay/notify（验签+幂等入账）
│   ├── POST /suggestion（用户提意见）；GET /suggestions（管理员列表）；POST /suggestions/delete（人工删）；POST /suggestions/filter（LLM 删无意义，提示词可配）
│   ├── GET|POST /llm/filter-prompt（意见筛选提示词读写；[llm] suggestion_filter_prompt；设置页编辑框）
│   ├── GET /client/app-update → version / main_url / update_url / label
│   ├── POST /auth/web-ticket ；POST /auth/consume-ticket
│   ├── GET /ui → recharge_url / register_url / cookie_guide_url / db_admin_url / link_harvest_*
│   ├── GET /shops ；GET /shops/source ；POST /shops/delete
│   ├── GET /scan-shops ；POST /scan-shops/add|delete
│   ├── POST /auto-scan/enable ；POST /admin/auto-scan-fee
│   └── GET /admin/db/tables ；GET /admin/db/rows
├── db.py
│   ├── create_user / set_user_password（改密作废全部 session）/ set_user_phone / list_usernames_by_phone
│   ├── create_session_row / get_session_row / delete_session_row / delete_sessions_for_user
│   ├── rename_user（2-32 位字母数字下划线中文；唯一性校验；同名幂等返回）
│   ├── record_user_login（最近登录 IP/城市/时间；IP 为空直接报错）
│   ├── create_pay_order / get_pay_order / mark_pay_order_paid（仅 pending→paid 入账一次，防重复回调）
│   ├── add_suggestion / list_suggestions（带用户名）/ delete_suggestion / delete_suggestions
│   ├── save_sms_code / consume_sms_code / delete_sms_codes
│   ├── enable_auto_scan / change_balance
│   ├── list_scan_shops / shop_already_in_db / add_scan_shop / delete_scan_shop
│   ├── get_shop / get_shop_by_name / set_shop_source_md / get_shop_source_md
│   ├── upsert_goods_db（problem 为空直接报错，不收没问题商品）/ delete_goods_by_item_ids（重扫改好的出表）
│   ├── refresh_shop_counts（goods_sum 只认上报值）/ cleanup_goods_tb（启动清历史空问题行）
│   ├── admin_db_tables / admin_db_rows
│   └── delete_shop_db
├── sms.py
│   └── send_sms_code（阿里云；缺配置直接报错）
├── payments.py                # 微信 Native / 支付宝电脑网站支付网关（商户密钥读 local；与 livestream-api 共享）
├── pay_config.py              # [pay] 段读取：enabled / 商户号 / 密钥路径（缺配置直接报错）
├── client/
│   ├── app.py
│   │   ├── 单店：链接暗字 + 开始抓取（无采链/全自动）
│   │   ├── 顶栏「模式」下拉：单店模式 / 自动模式；select_ui_mode / apply_ui_mode；apply_auto_kind（全自动置灰「获取数量/获取店铺」）
│   │   ├── 全自动/半自动单选钮暗色样式（TRadiobutton/TCheckbutton 无底色方块）
│   │   ├── upload_scan_results：只上传 problem 非空商品；全量 id/标题/总数走 shop.item_ids|item_titles|goods_sum
│   │   ├── 查看待检店铺 open_pending_window（excel + 下载）；开始扫描 run_auto_random
│   │   ├── 全自动：列表空后续采；半自动：只扫当前列表
│   │   ├── harvest_to_cloud / drop_pending_shop / fetch_and_scan_one
│   │   ├── ensure_local_shop_md（看源文件/分析时本地没有才下云端）
│   │   ├── _focus_shop_window / _register_shop_window（已扫店/源文件/登录/设置单例）
│   │   ├── shops_for_action（勾选优先，否则点选行）/ fill_shop_link_box
│   │   ├── apply_account_paths / open_recharge（web-ticket）；菜单「注册」打开 register_url（?tab=register 直达注册页）；open_suggestion_dialog「提意见…」单例窗（说明+文本框+提交 → POST /suggestion）
│   │   ├── set_token_refresh / login client:true（不过期会话；401 静默重登）
│   │   ├── 已扫：图标按钮+悬停说明；右键删除；总复选框全选；resolve_rescan_targets；双击/各窗单例（_register_shop_window 返回关闭函数）
│   │   ├── 设置：harvest_count / shops_dir（默认 file/<用户>/）
│   │   └── 启动静默查更新：有新版本弹窗问是否升级（start_update_download 与菜单「检查更新」共用），菜单栏「有新版本」入口保留
│   ├── shop_store.py          # user_file_dir / default_shops_dir(username) / match_local_shop_md / write_shop_md_text
│   ├── ui_icons.py            # load_action_photos / HoverTip
│   ├── shop_pipeline.py       # export_problems_xlsx / export_pending_shops_xlsx / module_scan_save_md
│   ├── link_queue.py          # queue_path → 用户 data/link_queue.json
│   ├── link_harvest.py        # load_harvest_config（GET /ui 覆盖） / harvest_into_queue
│   ├── text_menu.py           # bind_text_context_menu：剪切复制粘贴删除全选
│   ├── ui_layout.py           # [layout] v4；clamp；split_md_products 跳过登录标题
│   ├── app_update.py          # fetch_app_update_info / download_and_launch_update
│   ├── chrome_fetch.py        # Chrome 配置在用户目录；fetch_item 拒登录页 / collect_item_ids
│   ├── md_highlight.py        # highlight_markdown 源码着色
│   └── version.py             # APP_TITLE / CLIENT_VERSION 1.6.1
├── packaging/
│   ├── absolute_onedir.spec   # 硬拷 RapidOCR onnx+config.yaml；collect_all cv2；datas: 词表/logo/config.ini
│   ├── build_onedir.ps1
│   ├── absolute.iss           # AppName=小李的电商扫描器；{app}\file users-modify；exe 仍 极限词扫描.exe
│   ├── build_installer.ps1 [-Publish]
│   └── out/absolute_term/Setup.exe
├── www/
│   ├── index.html             # consumeTicketFromUrl 一次性票登录；充值/登录/注册；用户下拉菜单（行内改名/充值/手机/自动扫描弹窗开通，无独立页）；管理员下拉菜单 ⚙（设置/数据库/LLM提示词/意见库 openSuggestionsDialog，按 [auth] admin_user_id 锚定）；?tab=register|reset|recover 强制切页签
│   ├── js/dialog.js           # AppDialog / AppDialog.confirm → UiConfirm
│   ├── guide.html             # Cookie 获取说明（静态、免登录）
│   ├── db.html                # 管理员只读数据库页（新开）
│   ├── img/cookie_guide.png
│   └── downloads/
├── config.ini
│   └── [ui] register_url（?tab=register）/ cookie_guide_url / db_admin_url
│       [client] link_harvest_* / auto_random_*
│       [billing] scan_fee_yuan / auto_scan_fee_yuan
│       [auth] sms_ttl_seconds / sms_resend_seconds / sms_code_length / web_ticket_ttl_seconds / admin_user_id（管理员按 user_tb.id 锚定，不认用户名）
│       [geo] enabled / api_url / city_json_keys / timeout_seconds（登录 IP 归属城市）
│       [pay] enabled 等（充值总开关；商户密钥仅 local）
│       [aliyun_sms] enabled / sign_name / template_code（AK/SK 仅 local）
│       [client_release]
└── promt.md / tree.md
```
