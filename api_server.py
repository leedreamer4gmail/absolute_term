#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极限词扫描 API：Unix socket，由 nginx 反代到 /absolute_term/api/。"""

from __future__ import annotations

import base64
import configparser
import csv
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.ini"
CONFIG_LOCAL_PATH = ROOT / "config.ini.local"
COOKIE_PATH = ROOT / "data" / "cookie.txt"
SOCK_PATH = Path(os.environ.get("ABSOLUTE_SOCK", "/run/absolute/absolute.sock"))
MAX_UPLOAD = 100 * 1024 * 1024  # 100MB
# 本地 OCR 服务(服务器 ocr_server.py 监听 8799)；可由 config.ini [ocr] ocr_base 覆盖
OCR_BASE = os.environ.get("ABSOLUTE_OCR", "http://127.0.0.1:8799")

# 在线编辑词表：file/absolute_words.md + file/wrong_word.md
FILE_KEYS = {
    "limit": ("words", "limit_file", "file/absolute_words.md", "极限词"),
    "wrong": ("words", "wrong_file", "file/wrong_word.md", "错误描述"),
}

# 后台扫描任务进度表 {task_id: {...}}
TASKS: dict[str, dict] = {}
_TASK_LOCK = threading.Lock()

# 登录会话已落库 session_tb（重启不丢）。client 不过期；web 看 session_ttl。
# 客户端打开网页用的一次性登录票 {ticket: {user_id, exp}}
WEB_TICKETS: dict[str, dict] = {}
_TICKET_LOCK = threading.Lock()

# 允许网页/客户端改写的配置项（settings 面板）
SETTINGS_EDITABLE = {
    ("ui", "houtai_url"): "返回后台链接",
    ("ui", "home_url"): "公司主页链接",
    ("ui", "public_base"): "站点路径前缀",
    ("ui", "logo_file"): "客户端主 Logo 路径（相对仓库根，如 file/img/logo.jpg）",
    ("ui", "logo_icon_file"): "客户端窗口图标路径（相对仓库根，如 file/img/logox.jpg）",
    ("ui", "recharge_url"): "客户端点击余额打开的充值页 URL（须含 #recharge）",
    ("ui", "cookie_guide_url"): "客户端「如何获取 Cookie」打开的说明页 URL（免登录静态页，如 …/guide.html）",
    ("ui", "register_url"): "客户端菜单「注册」打开的网页 URL（须带 ?tab=register）",
    ("ui", "db_admin_url"): "管理员顶栏「数据库」新页面 URL（仅管理员，须能新开页看库）",
    ("client_release", "client_app_version"): "客户端对外版本号（与安装包同号）",
    ("client_release", "download_main_url"): "完整安装包下载 URL",
    ("client_release", "download_main_label"): "完整安装包标题",
    ("client_release", "download_main_note"): "完整安装包说明",
    ("client_release", "download_update_url"): "升级包下载 URL（检查更新用）",
    ("client_release", "download_update_note"): "升级包说明",
    ("client", "default_api"): "本机客户端默认 API",
    ("client", "item_delay_seconds"): "本机抓取基础间隔秒数",
    ("client", "item_delay_min_seconds"): "自适应间隔下限秒数",
    ("client", "item_delay_max_seconds"): "自适应间隔上限秒数（挤爆时拉到此值）",
    ("client", "wind_backoff_factor"): "每次挤爆后间隔乘数",
    ("client", "wind_control_pause_after"): "连续风控几次后暂停",
    ("client", "wind_control_pause_seconds"): "风控暂停秒数",
    ("client", "taobao_login_url"): "本机浏览器打开的淘宝登录地址",
    ("client", "auto_slider"): "是否尝试自动拖淘宝滑块（1/0）",
    ("client", "chrome_login_wait_seconds"): "开始抓取时等待专用 Chrome 登录淘宝的最长秒数",
    ("client", "link_harvest_url"): "自动采链先打开的淘宝页（从中抽商品 id）",
    ("client", "link_harvest_search_url"): "首页商品不足时的搜索页，必须含 {q}",
    ("client", "link_harvest_keyword"): "采链搜索关键词，逗号分隔则每次随机抽一个",
    ("client", "link_harvest_count"): "一次采链入队多少个未扫过店铺的商品链接",
    ("client", "link_harvest_max_try"): "采链最多尝试多少个商品 id（含已扫跳过）",
    ("client", "link_harvest_wait_seconds"): "每个采链页等待渲染/滚动的秒数",
    ("client", "auto_random_max_shops"): "自动随机扫最多几家店，0=直到点停止",
    ("client", "auto_random_pause_seconds"): "自动随机扫两家店之间额外等待秒数",
    ("client", "auto_random_popup_problems"): "自动随机扫是否每店弹问题窗（1/0，建议 0）",
    ("billing", "scan_fee_yuan"): "每扫描 1 条商品扣费（元），0=免费",
    ("billing", "auto_scan_fee_yuan"): "永久打开自动扫描的一次性费用（元），0=免费开通",
    ("scan", "default_max_items"): "默认扫描商品上限，0=不限",
    ("scan", "item_delay_seconds"): "服务器重扫商品间隔秒数",
    ("scan", "wind_control_pause_after"): "服务器连续风控几次后暂停",
    ("scan", "wind_control_pause_seconds"): "服务器风控暂停秒数",
    ("image", "sample_count"): "抽查主图商品数量上限",
    ("image", "main_ocr_count"): "主图 OCR 张数上限",
    ("image", "detail_ocr_count"): "详情图 OCR 张数上限",
    ("ocr", "engine"): "OCR 引擎",
    ("ocr", "confidence"): "OCR 置信度阈值",
    ("ocr", "ocr_base"): "本地 OCR 服务地址",
    ("llm", "api_url"): "LLM API 地址（SiliconFlow）",
    ("llm", "model"): "LLM 模型名（deepseek-ai/DeepSeek-V4-Flash）",
    ("auth", "session_ttl_seconds"): "网页登录会话有效秒数（仅 channel=web；客户端 token 不过期，只在退出/改密时作废）",
    ("auth", "sms_ttl_seconds"): "短信验证码有效秒数",
    ("auth", "sms_resend_seconds"): "同一手机同一用途最短重发间隔秒数",
    ("auth", "sms_code_length"): "短信验证码位数",
    ("auth", "web_ticket_ttl_seconds"): "客户端打开网页时一次性登录票有效秒数",
    ("aliyun_sms", "enabled"): "是否启用阿里云短信（1/0）。AK/SK 只能写 config.ini.local",
    ("aliyun_sms", "sign_name"): "阿里云短信签名",
    ("aliyun_sms", "template_code"): "阿里云短信模板 CODE（须含 ${code}）",
    ("geo", "enabled"): "是否记录登录 IP 归属城市（1/0；关闭后只记 IP 不查城市）",
    ("geo", "api_url"): "IP 归属地查询接口，{ip} 会被替换为登录 IP（ip-api.com 免费版只支持 http）",
    ("geo", "city_json_keys"): "从归属地接口返回 JSON 里按顺序取城市，逗号分隔，取第一个非空字段",
    ("geo", "timeout_seconds"): "归属地查询超时秒数（超时/失败只留空城市，不影响登录）",
    ("pay", "enabled"): "充值总开关（1=开放微信/支付宝充值，0=关闭）",
}

# 管理员锚定：只认 user_tb.id（config.ini [auth] admin_user_id），不认用户名；
# 未配置或配置为空时没有任何管理员
def _admin_user_id() -> str:
    cfg = _read_config()
    return (cfg.get("auth", "admin_user_id", fallback="") or "").strip()


def _is_admin_user(user: dict | None) -> bool:
    uid = str((user or {}).get("id") or "").strip()
    admin_id = _admin_user_id()
    if not uid or not admin_id:
        return False
    return uid == admin_id

DEFAULT_JUDGE_PROMPT = (
    "你是电商广告合规审核员。下面商品文本命中了极限词或错误描述候选, "
    "请判断哪些构成《广告法》绝对化用语违规或虚假宣传, 哪些是正常表述。\n"
    "判定原则：\n"
    "1. 只有宣称本品优于一切/无人能及/行业第一等「绝对化推销」才算违规，"
    "例如：最好、第一、天花板、最便宜、独一无二（吹效果）。\n"
    "2. 单字「最/第」等出现在客观规格、数量、顺序、时间里不算违规。"
    "例如：包装最小规格、最小起订量、最大承重、最新生产日期、"
    "最多可优惠40元、最后一件库存、最初配方、最高立减。\n"
    "3. 对每条命中都必须输出一条 violations，violate 填 true 或 false，并写简短 reason。\n\n"
    "商品标题: {title}\n链接: {url}\n\n{hits_block}\n{rules}"
    "只输出一个 JSON 对象，不要 markdown，不要解释。"
    '格式: {"violations":[{"keyword":"...","source":"...","violate":true,"reason":"..."}]}'
)


from llm_config import load_llm_config  # noqa: E402
from cookie_util import (  # noqa: E402
    _normalize_cookie,
    pick_best_cookie as _pick_best_cookie_raw,
    validate_cookie,
)
from db import (  # noqa: E402
    DbError,
    add_scan_shop,
    add_suggestion,
    authenticate,
    change_balance,
    consume_sms_code,
    create_pay_order,
    create_session_row,
    create_user,
    delete_goods_by_item_ids,
    delete_session_row,
    delete_scan_shop,
    delete_shop_db,
    delete_sms_codes,
    delete_suggestion,
    delete_suggestions,
    enable_auto_scan,
    ensure_test_user,
    get_session_row,
    get_shop as db_get_shop,
    get_shop_by_name as db_get_shop_by_name,
    get_pay_order,
    get_user_by_id,
    get_user_by_username,
    health_db,
    init_schema,
    list_goods,
    list_scan_shops,
    list_shops as db_list_shops,
    list_suggestions,
    list_usernames_by_phone,
    mark_pay_order_paid,
    record_user_login,
    refresh_shop_counts,
    rename_user,
    save_sms_code,
    save_user_cookie,
    set_shop_source_md,
    get_shop_source_md,
    admin_db_tables,
    admin_db_rows,
    set_user_password,
    set_user_phone,
    shop_already_in_db,
    upsert_goods_db,
    upsert_shop_db,
    upsert_shop_uniq,
    _norm_phone,
)
from sms import SmsError, send_sms_code  # noqa: E402


def pick_best_cookie(text: str, use_llm: bool = True) -> dict:
    return _pick_best_cookie_raw(text, use_llm=use_llm, llm_config_path=CONFIG_PATH)


class UnixHTTPServer(HTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self) -> None:
        if os.path.exists(self.server_address):
            os.unlink(self.server_address)
        super().server_bind()
        os.chmod(self.server_address, 0o666)


def _read_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(f"缺少配置文件: {CONFIG_PATH}")
    cfg.read(str(CONFIG_PATH), encoding="utf-8")
    if CONFIG_LOCAL_PATH.is_file():
        cfg.read(str(CONFIG_LOCAL_PATH), encoding="utf-8")
    return cfg


def _session_ttl() -> int:
    cfg = _read_config()
    return cfg.getint("auth", "session_ttl_seconds", fallback=604800)


def _sms_ttl() -> int:
    cfg = _read_config()
    return cfg.getint("auth", "sms_ttl_seconds", fallback=300)


def _sms_resend() -> int:
    cfg = _read_config()
    return cfg.getint("auth", "sms_resend_seconds", fallback=60)


def _sms_code_length() -> int:
    cfg = _read_config()
    n = cfg.getint("auth", "sms_code_length", fallback=6)
    if n < 4 or n > 8:
        raise ValueError("sms_code_length 须在 4～8")
    return n


def _login_payload(user: dict, *, channel: str = "web") -> dict:
    token = create_session(user, channel=channel)
    full = get_user_by_id(int(user["id"])) or user
    return {
        "ok": True,
        "token": token,
        "user": public_me(full),
        "channel": channel,
    }


# IP 归属地默认接口（ip-api.com 免费版，仅 http；可用网页设置改）
DEFAULT_GEO_API_URL = "http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,country,regionName,city"


def _geo_enabled() -> bool:
    return _read_config().get("geo", "enabled", fallback="1").strip() == "1"


def lookup_ip_city(ip: str) -> str:
    """按 [geo] 配置查 IP 归属城市；查不到/失败返回空串，绝不猜测。"""
    ip = str(ip or "").strip()
    if not ip or not _geo_enabled():
        return ""
    cfg = _read_config()
    api_url = (cfg.get("geo", "api_url", fallback=DEFAULT_GEO_API_URL) or "").strip()
    if not api_url or "{ip}" not in api_url:
        raise ValueError("[geo] api_url 缺少 {ip} 占位符，无法查询归属地")
    keys = [
        k.strip()
        for k in (cfg.get("geo", "city_json_keys", fallback="city,regionName") or "").split(",")
        if k.strip()
    ]
    if not keys:
        raise ValueError("[geo] city_json_keys 不能为空")
    try:
        timeout = float(cfg.get("geo", "timeout_seconds", fallback="3") or 3)
    except ValueError as e:
        raise ValueError(f"[geo] timeout_seconds 无效: {e}") from e
    if timeout <= 0:
        raise ValueError("[geo] timeout_seconds 须大于 0")
    url = api_url.replace("{ip}", urllib.parse.quote(ip))
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        return ""
    if str(data.get("status") or "").lower() == "fail":
        return ""
    for k in keys:
        v = str(data.get(k) or "").strip()
        if v:
            return v
    return ""


def record_login_async(user_id: int, ip: str) -> None:
    """后台线程查归属地并写 user_tb；任何失败只写日志，不影响登录。"""
    if not user_id or not str(ip or "").strip():
        return

    def job() -> None:
        try:
            city = lookup_ip_city(ip)
        except Exception as e:  # noqa: BLE001
            city = ""
            print(f"[geo] 查询 {ip} 失败: {e}", file=sys.stderr)
        try:
            record_user_login(int(user_id), ip, city)
        except Exception as e:  # noqa: BLE001
            print(f"[geo] 记录登录失败 user={user_id}: {e}", file=sys.stderr)

    t = threading.Thread(target=job, daemon=True)
    t.start()


# ---------- 充值（微信 Native / 支付宝电脑网站支付） ----------

def _new_order_no() -> str:
    return f"AT{int(time.time() * 1000)}{secrets.token_hex(3).upper()}"


def pay_recharge_api(user: dict, payload: dict) -> dict:
    """下单：返回 order_no + 微信 code_url / 支付宝 pay_url。"""
    import payments

    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    if _read_config().get("pay", "enabled", fallback="1").strip() != "1":
        raise ValueError("充值功能暂未开放")
    channel = str(payload.get("channel") or "").strip()
    if channel not in ("wechat", "alipay"):
        raise ValueError("channel 只能是 wechat 或 alipay")
    try:
        amount_cents = int(payload.get("amount_cents") or 0)
    except (TypeError, ValueError) as e:
        raise ValueError(f"amount_cents 必须是整数: {e}") from e
    if amount_cents < 10:
        raise ValueError("最低充值 0.1 元")
    if amount_cents > 10000000:
        raise ValueError("单笔充值不能超过 10 万元")

    order_no = _new_order_no()
    create_pay_order(int(user["id"]), order_no, channel, amount_cents)
    desc = "小李的电商扫描器-余额充值"

    if channel == "wechat":
        r = payments.wxpay_create_native(amount_cents, order_no, desc)
        if "error" in r:
            raise ValueError(r["error"])
        return {"ok": True, "order_no": order_no, "amount_cents": amount_cents,
                "channel": "wechat", "code_url": r["code_url"]}
    r = payments.zfb_build_page_pay_url(order_no, amount_cents, desc)
    if "error" in r:
        raise ValueError(r["error"])
    return {"ok": True, "order_no": order_no, "amount_cents": amount_cents,
            "channel": "alipay", "pay_url": r["pay_url"]}


def _sync_order_from_gateway(order: dict) -> dict:
    """pending 订单主动查网关；已付则幂等入账。返回最新 order。"""
    import payments

    if order["status"] != "pending":
        return order
    trade_no = ""
    paid = False
    if order["channel"] == "wechat":
        q = payments.wxpay_query(order["order_no"])
        if q.get("trade_state") == "SUCCESS":
            paid = True
            trade_no = str((q.get("raw") or {}).get("transaction_id") or "")
    else:
        q = payments.zfb_query(order["order_no"])
        st = str(q.get("trade_status") or "")
        if st in ("TRADE_SUCCESS", "TRADE_FINISHED"):
            paid = True
            trade_no = ""
    if paid:
        mark_pay_order_paid(order["order_no"], trade_no)
        order = get_pay_order(order["order_no"]) or order
    return order


def pay_order_status_api(user: dict, order_no: str) -> dict:
    order = get_pay_order(order_no)
    if not order:
        raise ValueError("订单不存在")
    if int(order["user_id"]) != int(user["id"]):
        raise PermissionError("无权查看该订单")
    order = _sync_order_from_gateway(order)
    return {"ok": True, "order_no": order["order_no"], "status": order["status"],
            "amount_cents": order["amount_cents"], "channel": order["channel"]}


def pay_wechat_notify(body: bytes, headers) -> tuple[bool, str]:
    """微信回调：验签解密 → 幂等入账。返回 (成功?, 回复说明)。"""
    import payments

    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return False, "body 非 JSON"
    if not payments.wxpay_verify_notify(headers, body):
        return False, "签名校验失败"
    res = data.get("resource") or {}
    try:
        plain = payments.wxpay_aes_decrypt(
            payments.wxpay_cfg()["api_v3_key"],
            str(res.get("associated_data") or ""),
            str(res.get("nonce") or ""),
            str(res.get("ciphertext") or ""),
        )
        obj = json.loads(plain)
    except Exception:
        return False, "解密失败"
    if str(obj.get("trade_state") or "") != "SUCCESS":
        return True, "非成功状态,忽略"
    order_no = str(obj.get("out_trade_no") or "")
    trade_no = str(obj.get("transaction_id") or "")
    mark_pay_order_paid(order_no, trade_no)
    return True, "OK"


def pay_alipay_notify(form: dict) -> tuple[bool, str]:
    """支付宝回调：验签 → 幂等入账。"""
    import payments

    if not payments.zfb_verify(form):
        return False, "签名校验失败"
    st = str(form.get("trade_status") or "")
    if st not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return True, "非成功状态,忽略"
    order_no = str(form.get("out_trade_no") or "")
    trade_no = str(form.get("trade_no") or "")
    mark_pay_order_paid(order_no, trade_no)
    return True, "success"


def make_qr_png(data: str) -> bytes:
    """把 code_url 渲染成 PNG 二维码。"""
    import io
    import qrcode

    data = str(data or "").strip()
    if not data or len(data) > 2048:
        raise ValueError("qr data 无效")
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def register_api(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    user = create_user(
        str(payload.get("username") or ""),
        str(payload.get("password") or ""),
        str(payload.get("phone") or ""),
    )
    return _login_payload(user)


def send_sms_api(payload: dict, user: dict | None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    purpose = str(payload.get("purpose") or "").strip()
    phone = _norm_phone(str(payload.get("phone") or ""))
    username = str(payload.get("username") or "").strip()
    if purpose not in ("reset", "recover", "bind"):
        raise ValueError("短信用途须为 reset / recover / bind")
    if not phone:
        raise ValueError("请填写手机号")
    if purpose == "reset":
        if not username:
            raise ValueError("请填写用户名")
        target = get_user_by_username(username)
        if not target:
            raise ValueError("用户不存在")
    elif purpose == "recover":
        names = list_usernames_by_phone(phone)
        if not names:
            raise ValueError("这个手机号下还没有账号。用「重置密码」填这个手机即可，不必事先绑定")
    else:
        if not user:
            raise PermissionError("请先登录再绑定手机")
    length = _sms_code_length()
    code = "".join(str(secrets.randbelow(10)) for _ in range(length))
    save_sms_code(phone, purpose, code, _sms_ttl(), _sms_resend())
    try:
        send_sms_code(phone, code)
    except Exception:
        delete_sms_codes(phone, purpose)
        raise
    return {"ok": True}


def reset_password_api(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    username = str(payload.get("username") or "").strip()
    phone = _norm_phone(str(payload.get("phone") or ""))
    code = str(payload.get("code") or "").strip()
    new_password = str(payload.get("new_password") or "")
    if not username:
        raise ValueError("请填写用户名")
    if not phone:
        raise ValueError("请填写手机号")
    if not code:
        raise ValueError("请填写验证码")
    target = get_user_by_username(username)
    if not target:
        raise ValueError("用户不存在")
    consume_sms_code(phone, "reset", code)
    set_user_password(int(target["id"]), new_password)
    set_user_phone(int(target["id"]), phone)
    full = get_user_by_id(int(target["id"]))
    if not full:
        raise ValueError("重置后读不到用户")
    return _login_payload(full)


def recover_usernames_api(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    phone = _norm_phone(str(payload.get("phone") or ""))
    code = str(payload.get("code") or "").strip()
    if not phone:
        raise ValueError("请填写手机号")
    if not code:
        raise ValueError("请填写验证码")
    consume_sms_code(phone, "recover", code)
    names = list_usernames_by_phone(phone)
    return {"ok": True, "usernames": names, "count": len(names)}


def bind_phone_api(user: dict, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    phone = _norm_phone(str(payload.get("phone") or ""))
    code = str(payload.get("code") or "").strip()
    if not phone:
        raise ValueError("请填写手机号")
    if not code:
        raise ValueError("请填写验证码")
    consume_sms_code(phone, "bind", code)
    full = set_user_phone(int(user["id"]), phone)
    return {"ok": True, "user": public_me(full)}


def rename_user_api(user: dict, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    new_name = str(payload.get("username") or "").strip()
    full = rename_user(int(user["id"]), new_name)
    return {"ok": True, "user": public_me(full)}


def _web_ticket_ttl() -> int:
    n = _read_config().getint("auth", "web_ticket_ttl_seconds", fallback=120)
    if n < 30 or n > 600:
        raise ValueError("web_ticket_ttl_seconds 须在 30～600")
    return n


def issue_web_ticket(user: dict) -> dict:
    ticket = secrets.token_urlsafe(24)
    ttl = _web_ticket_ttl()
    with _TICKET_LOCK:
        WEB_TICKETS[ticket] = {"user_id": int(user["id"]), "exp": time.time() + ttl}
    return {"ok": True, "ticket": ticket, "ttl": ttl}


def consume_web_ticket(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    ticket = str(payload.get("ticket") or "").strip()
    if not ticket:
        raise ValueError("缺少登录票")
    with _TICKET_LOCK:
        rec = WEB_TICKETS.pop(ticket, None)
    if rec is None:
        raise ValueError("登录票无效或已使用")
    if rec["exp"] < time.time():
        raise ValueError("登录票已过期，请从客户端重新打开网页")
    user = get_user_by_id(int(rec["user_id"]))
    if not user:
        raise ValueError("用户不存在")
    return _login_payload(user)


def create_session(user: dict, *, channel: str = "web") -> str:
    """写 session_tb。channel=client 永不过期（直至退出/改密）；web 用 session_ttl。"""
    channel = str(channel or "web").strip().lower()
    if channel not in ("client", "web"):
        raise ValueError(f"未知会话 channel: {channel}")
    token = secrets.token_urlsafe(32)
    ttl = None if channel == "client" else _session_ttl()
    create_session_row(
        int(user["id"]),
        token,
        channel=channel,
        ttl_seconds=ttl,
    )
    return token


def destroy_session(token: str) -> None:
    delete_session_row(token)


def resolve_session(token: str) -> dict | None:
    row = get_session_row(token)
    if not row:
        return None
    user = get_user_by_id(int(row["user_id"]))
    if not user:
        delete_session_row(token)
        return None
    return {
        "user_id": int(user["id"]),
        "username": user["username"],
        "channel": row.get("channel") or "web",
    }


def _bearer_token(handler: BaseHTTPRequestHandler) -> str:
    auth = handler.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (handler.headers.get("X-Absolute-Token") or "").strip()


def require_user(handler: BaseHTTPRequestHandler) -> dict:
    token = _bearer_token(handler)
    sess = resolve_session(token)
    if not sess:
        raise PermissionError("未登录或会话已过期")
    user = get_user_by_id(int(sess["user_id"]))
    if not user or user.get("enable") != "T":
        raise PermissionError("用户不存在或已禁用")
    return {
        "id": user["id"],
        "username": user["username"],
        "cookie": user.get("cookie") or "",
        "token": token,
    }


def public_settings() -> dict:
    cfg = _read_config()
    items = []
    for (section, key), remark in SETTINGS_EDITABLE.items():
        if not cfg.has_section(section):
            continue
        items.append({
            "section": section,
            "key": key,
            "value": cfg.get(section, key, fallback=""),
            "remark": remark,
        })
    return {
        "ok": True,
        "items": items,
        "public_base": cfg.get("ui", "public_base", fallback="/absolute_term").strip(),
        "client": {
            "default_api": cfg.get("client", "default_api", fallback="").strip(),
            "item_delay_seconds": cfg.get("client", "item_delay_seconds", fallback="2.5"),
            "item_delay_min_seconds": cfg.get("client", "item_delay_min_seconds", fallback="2.0"),
            "item_delay_max_seconds": cfg.get("client", "item_delay_max_seconds", fallback="25.0"),
            "wind_backoff_factor": cfg.get("client", "wind_backoff_factor", fallback="1.8"),
            "wind_control_pause_after": cfg.get("client", "wind_control_pause_after", fallback="2"),
            "wind_control_pause_seconds": cfg.get("client", "wind_control_pause_seconds", fallback="60"),
            "taobao_login_url": cfg.get("client", "taobao_login_url", fallback="https://www.taobao.com/"),
            "auto_slider": cfg.get("client", "auto_slider", fallback="1"),
            "chrome_login_wait_seconds": cfg.get("client", "chrome_login_wait_seconds", fallback="180"),
        },
    }


def save_settings(updates: list[dict]) -> dict:
    if not isinstance(updates, list) or not updates:
        raise ValueError("updates 必须是非空数组")
    cfg = _read_config()
    # 写回主 ini，密钥段仍以 local 覆盖为准
    writable = configparser.ConfigParser()
    writable.optionxform = str
    writable.read(str(CONFIG_PATH), encoding="utf-8")
    changed = []
    for item in updates:
        if not isinstance(item, dict):
            raise ValueError("settings 项必须是对象")
        section = str(item.get("section") or "").strip()
        key = str(item.get("key") or "").strip()
        if (section, key) not in SETTINGS_EDITABLE:
            raise ValueError(f"不允许修改的配置: [{section}] {key}")
        if "value" not in item:
            raise ValueError(f"缺少 value: [{section}] {key}")
        value = str(item.get("value"))
        if not writable.has_section(section):
            writable.add_section(section)
        writable.set(section, key, value)
        changed.append({"section": section, "key": key, "value": value})
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        writable.write(f)
    return {"ok": True, "changed": changed, **public_settings()}


def run_scan(ocr: bool = True, llm: bool = True) -> str:
    """调用 scan.py 执行扫描，返回报告内容。"""
    import subprocess
    cmd = [sys.executable, str(ROOT / "scan.py")]
    if ocr:
        cmd.append("--ocr")
    if llm:
        cmd.append("--llm")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"扫描失败: {result.stderr[:500]}")
    # 读报告
    out_path = _read_config().get("scan", "output_file", fallback="output/违规清单.txt")
    report = (ROOT / out_path).read_text(encoding="utf-8")
    return report


def save_uploaded_csv(content: str) -> dict:
    """保存上传的 CSV 到 data/items.csv。"""
    csv_path = ROOT / "data" / "items.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(content, encoding="utf-8")
    reader = csv.DictReader(io.StringIO(content))
    count = sum(1 for _ in reader)
    return {"ok": True, "count": count, "path": str(csv_path)}


def save_uploaded_images(zip_data: bytes) -> dict:
    """解压上传的 ZIP 图片到 images 目录。"""
    img_dir = ROOT / "images"
    count = 0
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        for name in zf.namelist():
            if not name.endswith(("/", "\\")):
                target = (img_dir / name).resolve()
                if str(target).startswith(str(img_dir.resolve())):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
                    count += 1
    return {"ok": True, "count": count}


def load_cookie(user_id: int | None = None) -> str:
    """读取淘宝 Cookie；优先用户表，其次旧文件；无效内容视为未配置。"""
    if user_id:
        user = get_user_by_id(int(user_id))
        if user and (user.get("cookie") or "").strip():
            text = _normalize_cookie(user["cookie"])
            check = validate_cookie(text)
            if check.get("valid"):
                return text
    if not COOKIE_PATH.is_file():
        return ""
    text = _normalize_cookie(COOKIE_PATH.read_text(encoding="utf-8"))
    check = validate_cookie(text)
    return text if check.get("valid") else ""


def _cookie_saved_meta() -> dict:
    """只返回有证据的字段：文件保存时间。请求头 Cookie 不含 Expires/Max-Age。"""
    saved_at = 0
    if COOKIE_PATH.is_file():
        saved_at = int(COOKIE_PATH.stat().st_mtime)
    return {
        "server_now": int(time.time()),
        "saved_at": saved_at,
        "expire_known": False,
        "expire_at": None,
        "remaining_seconds": None,
    }


def cookie_status() -> dict:
    raw = ""
    if COOKIE_PATH.is_file():
        raw = _normalize_cookie(COOKIE_PATH.read_text(encoding="utf-8"))
    meta = _cookie_saved_meta()
    if not raw:
        return {"ok": True, "saved": False, "valid": False, "error": "未配置 Cookie", **meta}
    check = validate_cookie(raw)
    if check.get("valid"):
        return {
            "ok": True, "saved": True, "valid": True,
            "keys": check.get("keys", 0), "useful": check.get("useful", []),
            "has_h5_tk": check.get("has_h5_tk", False),
            "warn": check.get("warn") or "",
            "truncated_fields": check.get("truncated_fields") or [],
            **meta,
        }
    return {
        "ok": True, "saved": False, "valid": False,
        "error": check.get("error", "Cookie 无效"),
        **meta,
    }


def save_cookie(text: str, use_llm: bool = True, user_id: int | None = None) -> dict:
    """保存淘宝 Cookie。支持纯 Cookie / 单条或多条 curl，自动择优。"""
    picked = pick_best_cookie(text, use_llm=use_llm)
    if not picked.get("valid") or not picked.get("cookie"):
        return {**picked, **_cookie_saved_meta()}
    cookie = picked["cookie"]
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_PATH.write_text(cookie, encoding="utf-8")
    os.utime(COOKIE_PATH, None)
    if user_id:
        save_user_cookie(int(user_id), cookie)
    status = cookie_status()
    status.update({
        "ok": True,
        "saved": True,
        "valid": True,
        "user_id": int(user_id) if user_id else None,
        "score": picked.get("score"),
        "reasons": picked.get("reasons") or [],
        "pick_method": picked.get("pick_method"),
        "llm_note": picked.get("llm_note") or "",
        "source": picked.get("source") or "",
        "host": picked.get("host") or "",
        "candidates": picked.get("candidates") or [],
        "candidates_count": picked.get("candidates_count") or 0,
        "warn": picked.get("warn") or status.get("warn") or "",
        "rules": [
            "必须有 cookie2 + unb（登录态；document.cookie 通常没有 cookie2）",
            "uc1/uc3/uc4 长度≥20 才算完整；cookie15/nk2/nk4 视为残缺（常见于 detail.tmall.com）",
            "优先选择来自 h5api / www.taobao.com 且 uc* 完整的候选",
            "多条分数接近时再用 LLM 辅助（只看摘要，不上传完整 Cookie）",
        ],
    })
    return status


def _ocr_base() -> str:
    cfg = _read_config()
    return cfg.get("ocr", "ocr_base", fallback=OCR_BASE).rstrip("/")


def ocr_image_url(img_url: str, timeout: int = 45) -> list[str]:
    """调本地 OCR 服务识别图片文字，失败返回空。"""
    import requests
    try:
        r = requests.post(f"{_ocr_base()}/ocr_url", json={"url": img_url}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            return []
        return [str(t) for t in data.get("texts", [])]
    except Exception:  # noqa: BLE001
        return []


def _parse_words(text: str) -> list[str]:
    """词表按空白分隔（空格/换行/制表符），不再用 /。"""
    words: list[str] = []
    for piece in re.split(r"\s+", (text or "").strip()):
        piece = piece.strip()
        if piece and piece not in words:
            words.append(piece)
    return words


def _file_path(key: str) -> Path:
    if key not in FILE_KEYS:
        raise ValueError(f"未知词表: {key}")
    section, option, fallback, _label = FILE_KEYS[key]
    cfg = _read_config()
    rel = cfg.get(section, option, fallback=fallback).strip()
    path = (ROOT / rel).resolve()
    file_root = (ROOT / "file").resolve()
    if not str(path).startswith(str(file_root) + os.sep) and path.parent != file_root:
        raise ValueError("词表路径必须在 file/ 目录下")
    return path


def read_word_file(key: str) -> dict:
    """读取在线编辑词表原文。"""
    path = _file_path(key)
    label = FILE_KEYS[key][3]
    if not path.is_file():
        return {"ok": True, "key": key, "label": label, "path": str(path), "content": "", "words": [], "count": 0}
    content = path.read_text(encoding="utf-8")
    words = _parse_words(content)
    return {
        "ok": True,
        "key": key,
        "label": label,
        "path": str(path),
        "content": content,
        "words": words,
        "count": len(words),
        "updated_at": path.stat().st_mtime,
    }


def save_word_file(key: str, content: str) -> dict:
    """保存在线编辑词表原文到 file/。"""
    path = _file_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content is not None else "", encoding="utf-8")
    return read_word_file(key)


def load_word_groups() -> list[tuple[str, list[str]]]:
    """加载极限词 + 错误描述两组词表。"""
    groups: list[tuple[str, list[str]]] = []
    for key in ("limit", "wrong"):
        info = read_word_file(key)
        groups.append((info["label"], info["words"]))
    return groups


def files_status() -> dict:
    """两个词表的状态汇总。"""
    limit = read_word_file("limit")
    wrong = read_word_file("wrong")
    return {"ok": True, "limit": limit, "wrong": wrong}


def _goods_path() -> Path:
    cfg = _read_config()
    rel = cfg.get("scan", "goods_file", fallback="file/goods.md").strip()
    path = (ROOT / rel).resolve()
    file_root = (ROOT / "file").resolve()
    if not str(path).startswith(str(file_root) + os.sep) and path.parent != file_root:
        raise ValueError("goods 路径必须在 file/ 目录下")
    return path


def _parse_ocr_lines(section: str) -> list[str]:
    """解析「1. 图1：xxx」列表；忽略（无）。"""
    out: list[str] = []
    for line in (section or "").splitlines():
        s = line.strip()
        if not s or s == "（无）" or s.startswith("#"):
            continue
        mm = re.match(r"\s*\d+\.\s*图\d+[：:]\s*(.*)$", s)
        if mm:
            out.append(mm.group(1).strip())
        else:
            out.append(s)
    return out


def load_goods() -> dict[str, dict]:
    """解析 file/goods.md → {id: {index, title, main_ocr, detail_text, detail_ocr}}。"""
    path = _goods_path()
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    goods: dict[str, dict] = {}
    blocks = re.split(r"(?m)^# ", text)
    order = 0
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        head = lines[0].strip()
        # 支持「1. 商品名」序号标题
        m_num = re.match(r"^(\d+)\.\s*(.+)$", head)
        if m_num:
            index = int(m_num.group(1))
            title = m_num.group(2).strip()
        else:
            order += 1
            index = order
            title = head
        body = "\n".join(lines[1:])
        m_id = re.search(r"<!--\s*id:\s*(\d+)\s*-->", body)
        if not m_id:
            continue
        iid = m_id.group(1)
        main_ocr: list[str] = []
        m_main = re.search(r"##\s*主图文字\s*\n(.*?)(?=\n##\s|\Z)", body, re.S)
        if m_main:
            main_ocr = _parse_ocr_lines(m_main.group(1))
        detail_text = ""
        detail_ocr: list[str] = []
        m_detail = re.search(r"##\s*详情文字\s*\n(.*?)(?=\n##\s|\Z)", body, re.S)
        if m_detail:
            detail_body = m_detail.group(1).strip()
            # 详情文字区：纯文本 + 可选「N. 图N：」详情图 OCR
            text_lines: list[str] = []
            for line in detail_body.splitlines():
                s = line.strip()
                if not s or s == "（无）":
                    continue
                mm = re.match(r"\s*\d+\.\s*图\d+[：:]\s*(.*)$", s)
                if mm:
                    detail_ocr.append(mm.group(1).strip())
                else:
                    text_lines.append(s)
            detail_text = "\n".join(text_lines).strip()
        m_dimg = re.search(r"##\s*详情图文字\s*\n(.*?)(?=\n##\s|\Z)", body, re.S)
        if m_dimg:
            detail_ocr.extend(_parse_ocr_lines(m_dimg.group(1)))
        goods[iid] = {
            "id": iid,
            "index": index,
            "title": title,
            "main_ocr": main_ocr,
            "detail_text": detail_text,
            "detail_ocr": detail_ocr,
        }
        order = max(order, index)
    return goods


def render_goods_md(goods: dict[str, dict]) -> str:
    """把商品资料写成 goods.md 格式（带序号，主图/详情分开）。"""
    items = list(goods.values())
    items.sort(key=lambda g: int(g.get("index") or 0) or 10**9)
    # 无序号的按当前顺序补齐
    used = {int(g["index"]) for g in items if g.get("index")}
    next_i = 1
    for g in items:
        if not g.get("index"):
            while next_i in used:
                next_i += 1
            g["index"] = next_i
            used.add(next_i)
            next_i += 1
    items.sort(key=lambda g: int(g.get("index") or 0))

    parts: list[str] = []
    for g in items:
        iid = g.get("id") or ""
        title = (g.get("title") or iid).strip() or iid
        # 标题里若已带「N. 」则去掉，统一由序号字段控制
        title = re.sub(r"^\d+\.\s*", "", title).strip()
        idx = int(g.get("index") or 0) or 1
        parts.append(f"# {idx}. {title}")
        parts.append(f"<!-- id: {iid} -->")
        parts.append("")
        parts.append("## 主图文字")
        main_ocr = g.get("main_ocr") or []
        if main_ocr:
            for i, line in enumerate(main_ocr, 1):
                parts.append(f"{i}. 图{i}：{line}")
        else:
            parts.append("（无）")
        parts.append("")
        parts.append("## 详情文字")
        detail = (g.get("detail_text") or "").strip()
        detail_ocr = g.get("detail_ocr") or []
        if detail:
            parts.append(detail)
        if detail_ocr:
            if detail:
                parts.append("")
            for i, line in enumerate(detail_ocr, 1):
                parts.append(f"{i}. 图{i}：{line}")
        if not detail and not detail_ocr:
            parts.append("（无）")
        parts.append("")
    return "\n".join(parts).rstrip() + ("\n" if parts else "")


def migrate_goods_split() -> dict:
    """修复旧数据：把误写入主图的详情图 OCR 挪回详情文字。"""
    cfg = _read_config()
    main_n = cfg.getint("image", "main_ocr_count", fallback=2)
    goods = load_goods()
    moved = 0
    for g in goods.values():
        main_ocr = list(g.get("main_ocr") or [])
        detail_ocr = list(g.get("detail_ocr") or [])
        if len(main_ocr) > main_n and not detail_ocr:
            g["detail_ocr"] = main_ocr[main_n:]
            g["main_ocr"] = main_ocr[:main_n]
            moved += 1
        elif len(main_ocr) > main_n and detail_ocr:
            # 主图超长部分并入详情图（去重）
            extra = main_ocr[main_n:]
            g["main_ocr"] = main_ocr[:main_n]
            for line in extra:
                if line not in detail_ocr:
                    detail_ocr.append(line)
            g["detail_ocr"] = detail_ocr
            moved += 1
    if moved:
        # 重新编号 1..N
        for i, g in enumerate(goods.values(), 1):
            g["index"] = i
        save_goods(goods)
    return {"ok": True, "moved": moved, "count": len(goods)}


def save_goods(goods: dict[str, dict]) -> Path:
    path = _goods_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_goods_md(goods), encoding="utf-8")
    return path


def goods_status() -> dict:
    goods = load_goods()
    return {
        "ok": True,
        "path": str(_goods_path()),
        "count": len(goods),
        "ids": list(goods.keys())[:50],
    }


CLIENT_VERSION_FALLBACK = "1.0.0"


def _client_downloads_dir() -> Path:
    return ROOT / "www" / "downloads"


def client_info() -> dict:
    """本机抓取客户端下载信息（供页面展示）。"""
    ddir = _client_downloads_dir()
    version = CLIENT_VERSION_FALLBACK
    ver_file = ROOT / "client" / "version.py"
    if ver_file.is_file():
        m = re.search(r'CLIENT_VERSION\s*=\s*["\']([^"\']+)["\']', ver_file.read_text(encoding="utf-8"))
        if m:
            version = m.group(1)
    files = []
    for name in (
        "absolute_fetcher.exe",
        "absolute_fetcher_win.zip",
        "absolute_fetcher.zip",
        "README.txt",
    ):
        p = ddir / name
        if not p.is_file():
            continue
        h = ""
        try:
            import hashlib
            h = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            pass
        files.append({
            "name": name,
            "url": f"downloads/{name}",
            "size": p.stat().st_size,
            "sha256": h,
            "mtime": int(p.stat().st_mtime),
        })
    preferred = ""
    # Windows 便携包优先；exe 需在 Windows 上用 build_client.ps1 另打
    for n in ("absolute_fetcher.exe", "absolute_fetcher_win.zip", "absolute_fetcher.zip"):
        if any(f["name"] == n for f in files):
            preferred = f"downloads/{n}"
            break
    return {
        "ok": True,
        "version": version,
        "name": "absolute_fetcher",
        "preferred_download": preferred,
        "files": files,
        "steps": [
            "下载本机抓取客户端并解压/运行",
            "在客户端粘贴淘宝 Cookie（或读 Chrome），填写商品链接",
            "本机抓取 + OCR + 扫描完成后自动上传结果；网页「我的店铺库」查看",
        ],
        "note": "扫描在本机完成；云端只存店铺/问题商品到数据库。",
    }


def client_scan_bundle() -> dict:
    """给本机客户端的扫描物料：词表 + LLM。

    LLM 密钥只经 HTTPS 下发到客户端内存，客户端不得落盘；
    服务器侧密钥在 config.ini + config.ini.local（DeepSeek-V4-Flash）。
    """
    from llm_config import load_llm_config, normalize_chat_url

    limit = read_word_file("limit")
    wrong = read_word_file("wrong")
    llm = load_llm_config(require_complete=False)
    cfg = _read_config()
    if cfg.has_section("llm"):
        llm = {
            "api_key": (cfg.get("llm", "api_key", fallback="") or "").strip() or llm.get("api_key", ""),
            "api_url": normalize_chat_url(
                (cfg.get("llm", "api_url", fallback="") or "").strip() or llm.get("api_url", "")
            ),
            "model": (cfg.get("llm", "model", fallback="") or "").strip() or llm.get("model", ""),
        }
    if not llm.get("api_key"):
        raise ValueError(
            "服务器未配置 [llm] api_key（请写在 config.ini.local，模型用 deepseek-ai/DeepSeek-V4-Flash）"
        )
    if "DeepSeek" not in (llm.get("model") or "") and "deepseek" not in (llm.get("model") or "").lower():
        # 允许配置，但提示当前不是 flash 系时仍可用
        pass
    return {
        "ok": True,
        "limit_words": limit.get("words") or [],
        "wrong_words": wrong.get("words") or [],
        "limit_count": limit.get("count") or 0,
        "wrong_count": wrong.get("count") or 0,
        "llm": {
            "api_url": llm.get("api_url") or "",
            "model": llm.get("model") or "",
            "api_key": llm.get("api_key") or "",
            "has_key": bool(llm.get("api_key")),
            "judge_prompt": (
                (cfg.get("llm", "judge_prompt", fallback="") or "").strip()
                or DEFAULT_JUDGE_PROMPT
            ),
        },
        "image": {
            "main_ocr_count": cfg.getint("image", "main_ocr_count", fallback=2),
            "detail_ocr_count": cfg.getint("image", "detail_ocr_count", fallback=6),
        },
    }


def client_app_update_api(current: str = "") -> dict:
    """公开：客户端检查更新。完整包与升级包 URL 分开（笔记 953）。"""
    cfg = _read_config()
    remote = ""
    update_url = ""
    main_url = ""
    label = "小李的电商扫描器"
    note = ""
    if cfg.has_section("client_release"):
        remote = (cfg.get("client_release", "client_app_version", fallback="") or "").strip()
        update_url = (cfg.get("client_release", "download_update_url", fallback="") or "").strip()
        main_url = (cfg.get("client_release", "download_main_url", fallback="") or "").strip()
        label = (
            (cfg.get("client_release", "download_main_label", fallback="") or "").strip()
            or label
        )
        note = (cfg.get("client_release", "download_update_note", fallback="") or "").strip()
    if not remote:
        raise ValueError("云端未配置 [client_release] client_app_version")
    url = update_url or main_url
    if not url:
        raise ValueError("云端未配置 download_update_url（升级包）")
    cur = (current or "").strip() or "0.0.0"

    def _parse(v: str) -> tuple[int, ...]:
        m = re.match(r"^(\d+(?:\.\d+)*)", (v or "").strip())
        if not m:
            raise ValueError(f"无法解析版本号: {v!r}")
        return tuple(int(x) for x in m.group(1).split("."))

    try:
        available = _parse(remote) > _parse(cur)
    except ValueError:
        available = remote != cur
    return {
        "ok": True,
        "version": remote,
        "url": url,
        "main_url": main_url or url,
        "update_url": update_url or main_url,
        "label": label,
        "note": note,
        "update_available": available,
        "current": cur,
    }


def require_admin(handler: BaseHTTPRequestHandler) -> dict:
    user = require_user(handler)
    if not _is_admin_user(user):
        raise PermissionError("仅管理员可操作")
    return user


def _billing_fees() -> dict:
    cfg = _read_config()
    try:
        scan_yuan = float(cfg.get("billing", "scan_fee_yuan", fallback="0") or 0)
    except ValueError as e:
        raise ValueError("[billing] scan_fee_yuan 无法解析") from e
    try:
        auto_yuan = float(cfg.get("billing", "auto_scan_fee_yuan", fallback="0") or 0)
    except ValueError as e:
        raise ValueError("[billing] auto_scan_fee_yuan 无法解析") from e
    if scan_yuan < 0 or auto_yuan < 0:
        raise ValueError("费用不能为负")
    return {
        "scan_fee_yuan": scan_yuan,
        "scan_fee_cents": int(round(scan_yuan * 100)),
        "auto_scan_fee_yuan": auto_yuan,
        "auto_scan_fee_cents": int(round(auto_yuan * 100)),
    }


def _set_ini_option(section: str, key: str, value: str) -> None:
    writable = configparser.ConfigParser()
    writable.optionxform = str
    writable.read(str(CONFIG_PATH), encoding="utf-8")
    if not writable.has_section(section):
        writable.add_section(section)
    writable.set(section, key, value)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        writable.write(f)


def public_me(user: dict) -> dict:
    fees = _billing_fees()
    cfg = _read_config()
    public_base = (cfg.get("ui", "public_base", fallback="/absolute_term") or "").strip() or "/absolute_term"
    db_url = (cfg.get("ui", "db_admin_url", fallback="") or "").strip()
    if not db_url:
        db_url = public_base.rstrip("/") + "/db.html"
    out = {
        "ok": True,
        "id": user.get("id"),
        "username": user.get("username") or "",
        "has_cookie": bool((user.get("cookie") or "").strip()),
        "balance": float(user.get("balance") or 0),
        "auto_scan": bool(user.get("auto_scan")),
        "is_admin": _is_admin_user(user),
        "phone": user.get("phone") or "",
        **fees,
    }
    if out["is_admin"]:
        out["db_admin_url"] = db_url
    return out


def get_llm_prompt_api(user: dict) -> dict:
    if not _is_admin_user(user):
        raise PermissionError("仅管理员可查看/编辑 LLM 提示词")
    cfg = _read_config()
    prompt = (cfg.get("llm", "judge_prompt", fallback="") or "").strip() or DEFAULT_JUDGE_PROMPT
    return {
        "ok": True,
        "prompt": prompt,
        "placeholders": ["{title}", "{url}", "{hits_block}", "{rules}"],
        "remark": "写入 config.ini [llm] judge_prompt；客户端扫描时经 /client/bundle 下发到内存",
    }


def save_llm_prompt_api(user: dict, payload: dict) -> dict:
    if not _is_admin_user(user):
        raise PermissionError("仅管理员可查看/编辑 LLM 提示词")
    if not isinstance(payload, dict):
        raise ValueError("body 必须是对象")
    if "prompt" not in payload:
        raise ValueError("缺少 prompt")
    prompt = str(payload.get("prompt") or "")
    if not prompt.strip():
        raise ValueError("prompt 不能为空")
    # 必须能替换到占位符；JSON 花括号用 replace 不炸
    sample = prompt
    for key in ("{title}", "{url}", "{hits_block}", "{rules}"):
        if key in sample:
            sample = sample.replace(key, "x")
    writable = configparser.ConfigParser()
    writable.optionxform = str
    writable.read(str(CONFIG_PATH), encoding="utf-8")
    if not writable.has_section("llm"):
        writable.add_section("llm")
    writable.set("llm", "judge_prompt", prompt)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        writable.write(f)
    return get_llm_prompt_api(user)


# ---- 用户意见/建议（suggestion_tb）----

DEFAULT_SUGGESTION_FILTER_PROMPT = (
    "你是软件作者的助手。下面是用户提交的意见建议列表，每行格式为 [id] 内容。\n"
    "请判断每条对产品改进是否有价值：功能建议、体验问题、bug 描述等算有价值；"
    "乱码、无意义字符、纯表情、辱骂、随手测试占位（如 asdf、111、好）算毫无意义。\n"
    "只输出 JSON，不要别的字：{\"useless\": [id, ...]}，useless 里只填毫无意义条目的 id。"
    "拿不准的一律保留（不放进 useless）。"
)


def _current_filter_prompt(cfg) -> str:
    return (
        (cfg.get("llm", "suggestion_filter_prompt", fallback="") or "").strip()
        or DEFAULT_SUGGESTION_FILTER_PROMPT
    )


def get_filter_prompt_api(user: dict) -> dict:
    if not _is_admin_user(user):
        raise PermissionError("仅管理员可查看/编辑意见筛选提示词")
    return {"ok": True, "prompt": _current_filter_prompt(_read_config())}


def save_filter_prompt_api(user: dict, payload: dict) -> dict:
    if not _is_admin_user(user):
        raise PermissionError("仅管理员可查看/编辑意见筛选提示词")
    if not isinstance(payload, dict):
        raise ValueError("body 必须是对象")
    if "prompt" not in payload:
        raise ValueError("缺少 prompt")
    prompt = str(payload.get("prompt") or "")
    if not prompt.strip():
        raise ValueError("prompt 不能为空")
    if "{\"useless\"" not in prompt and "useless" not in prompt:
        raise ValueError("提示词里必须要求 LLM 输出 {\"useless\": [id, ...]} 格式")
    writable = configparser.ConfigParser()
    writable.optionxform = str
    writable.read(str(CONFIG_PATH), encoding="utf-8")
    if not writable.has_section("llm"):
        writable.add_section("llm")
    writable.set("llm", "suggestion_filter_prompt", prompt)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        writable.write(f)
    return get_filter_prompt_api(user)


def add_suggestion_api(user: dict, payload: dict) -> dict:
    """客户端「提意见」提交。"""
    comment = str((payload or {}).get("comment") or "")
    row = add_suggestion(int(user["id"]), comment)
    return {"ok": True, "id": row["id"], "date": row["date"]}


def list_suggestions_api(user: dict) -> dict:
    if not _is_admin_user(user):
        raise PermissionError("仅管理员可查看意见库")
    return {"ok": True, "items": list_suggestions()}


def delete_suggestion_api(user: dict, payload: dict) -> dict:
    if not _is_admin_user(user):
        raise PermissionError("仅管理员可删除意见")
    sid = (payload or {}).get("id")
    if sid is None:
        raise ValueError("缺少 id")
    if not delete_suggestion(int(sid)):
        raise ValueError("意见不存在或已被删除")
    return {"ok": True}


def filter_suggestions_api(user: dict) -> dict:
    """管理员一键筛选：LLM 判定毫无意义的意见直接删掉，有价值的保留。"""
    if not _is_admin_user(user):
        raise PermissionError("仅管理员可筛选意见")
    rows = list_suggestions(limit=500)
    if not rows:
        return {"ok": True, "deleted": 0, "deleted_ids": [], "items": [], "message": "意见库为空"}
    cfg = _read_config()
    conf = {
        "api_key": (cfg.get("llm", "api_key", fallback="") or "").strip(),
        "api_url": (cfg.get("llm", "api_url", fallback="") or "").strip(),
        "model": (cfg.get("llm", "model", fallback="") or "").strip(),
    }
    from llm_config import call_chat

    lines = "\n".join(f"[{r['id']}] {r['comment'][:200]}" for r in rows)
    text = call_chat(
        [
            {"role": "system", "content": _current_filter_prompt(cfg)},
            {"role": "user", "content": lines},
        ],
        temperature=0.0,
        timeout=120,
        conf=conf,
    )
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"LLM 返回无法解析：{text[:300]}")
    data = json.loads(m.group(0))
    valid_ids = {r["id"] for r in rows}
    useless: set[int] = set()
    for i in data.get("useless") or []:
        try:
            v = int(i)
        except (TypeError, ValueError):
            continue
        if v in valid_ids:
            useless.add(v)
    deleted_n = delete_suggestions(sorted(useless)) if useless else 0
    kept = [r for r in rows if r["id"] not in useless]
    return {"ok": True, "deleted": deleted_n, "deleted_ids": sorted(useless), "items": kept}


def import_scan_results(payload: dict, owner_user_id: int) -> dict:
    """接收本机已扫完的结果，写 shop_tb / goods_tb，并覆盖店铺源文件 source_md。

    goods_tb 只收「有问题」的商品：problem 为空的行不入库；本轮扫过且
    没问题的商品 id 会把该店历史问题记录删掉（重扫后改好了就出表）。
    店铺总商品数 goods_sum 由客户端按本轮实扫数上报，服务端不猜。
    """
    if not owner_user_id:
        raise ValueError("未登录，无法写入扫描结果")
    shop = payload.get("shop") or {}
    goods = payload.get("goods")
    if not isinstance(shop, dict):
        raise ValueError("shop 必须是对象")
    if goods is None:
        goods = []
    if not isinstance(goods, list):
        raise ValueError("goods 必须是数组")
    source_md = str(shop.get("source_md") or payload.get("source_md") or "")

    # 本轮全部扫过的商品 id/标题（客户端上报），用于店铺缓存与 goods_sum
    raw_ids = shop.get("item_ids")
    ids_all: list[str] = []
    if isinstance(raw_ids, list):
        ids_all = [str(x).strip() for x in raw_ids if str(x).strip()]
    raw_titles = shop.get("item_titles")
    titles_all: dict[str, str] = {}
    if isinstance(raw_titles, dict):
        titles_all = {
            str(k).strip(): str(v).strip()
            for k, v in raw_titles.items()
            if str(k).strip() and str(v).strip()
        }
    try:
        goods_sum_payload = int(shop.get("goods_sum") or 0)
    except (TypeError, ValueError) as e:
        raise ValueError(f"shop.goods_sum 必须是整数: {e}") from e
    if goods_sum_payload < 0:
        raise ValueError("shop.goods_sum 不能为负")

    rows: list[dict] = []
    for g in goods:
        if not isinstance(g, dict):
            continue
        iid = str(g.get("tb_item_id") or g.get("id") or "").strip()
        if not iid:
            continue
        name = str(g.get("goods_name") or g.get("title") or iid).strip() or iid
        link = str(g.get("goods_link") or g.get("url") or "").strip()
        if not link:
            link = f"https://item.taobao.com/item.htm?id={iid}"
        rows.append({
            "tb_item_id": iid,
            "goods_name": name,
            "goods_link": link,
            "problem": str(g.get("problem") or ""),
        })
    problem_rows = [r for r in rows if r["problem"].strip()]
    clean_ids = [r["tb_item_id"] for r in rows if not r["problem"].strip()]

    # 只更新源文件：没有任何商品数据可对账
    if not rows and not ids_all:
        if not source_md.strip():
            raise ValueError("goods 与 source_md 不能都空")
        tb_shop_id = str(shop.get("shop_id") or shop.get("tb_shop_id") or "").strip()
        existing = db_get_shop(int(owner_user_id), tb_shop_id) if tb_shop_id else None
        if not existing:
            name = str(shop.get("shop_name") or "").strip()
            if name:
                existing = db_get_shop_by_name(int(owner_user_id), name)
        if not existing:
            raise ValueError("店铺不存在，无法只更新源文件（本地有、云端无店名/id 对不上）")
        shop_row = set_shop_source_md(int(existing["id"]), source_md)
        return {
            "ok": True,
            "shop": shop_row,
            "upserted": 0,
            "deleted_clean": 0,
            "source_md_saved": True,
            "bad_goods_sum": int(shop_row.get("bad_goods_sum") or 0),
            "goods_sum": int(shop_row.get("goods_sum") or 0),
        }

    if not ids_all:
        ids_all = [r["tb_item_id"] for r in rows]
    for r in rows:
        if r["goods_name"] and r["goods_name"] != r["tb_item_id"]:
            titles_all.setdefault(r["tb_item_id"], r["goods_name"])

    tb_shop_id = str(shop.get("shop_id") or shop.get("tb_shop_id") or "").strip()
    seller_id = str(shop.get("user_id") or shop.get("seller_id") or "").strip()
    if not tb_shop_id or not seller_id:
        # 单品无店铺时用商品 id 占位，保证能落库
        iid = ids_all[0] if ids_all else ""
        if not iid:
            raise ValueError("缺少 shop_id/user_id，且 goods 无商品 id")
        tb_shop_id = tb_shop_id or f"item_{iid}"
        seller_id = seller_id or "0"

    goods_sum = goods_sum_payload or len(ids_all)
    shop_row = upsert_shop(
        int(owner_user_id),
        tb_shop_id,
        seller_id,
        shop_name=str(shop.get("shop_name") or ""),
        shop_url=str(shop.get("shop_link") or shop.get("shop_url") or ""),
        sample_item_id=ids_all[0],
        item_ids=ids_all,
        item_titles=titles_all or None,
    )
    shop_pk = int(shop_row["id"])
    # 本轮扫过且没问题的商品：从问题表里删掉历史记录（改好了就出表）
    deleted_clean = delete_goods_by_item_ids(shop_pk, clean_ids)
    upserted = []
    for r in problem_rows:
        saved = upsert_goods_db(
            shop_pk,
            r["tb_item_id"],
            goods_name=r["goods_name"],
            goods_link=r["goods_link"],
            problem=r["problem"],
        )
        upserted.append(saved)

    shop_row = refresh_shop_counts(
        shop_pk,
        item_ids=ids_all,
        item_titles=titles_all,
        shop_name=str(shop.get("shop_name") or ""),
        shop_link=str(shop.get("shop_link") or shop.get("shop_url") or ""),
        goods_sum=goods_sum,
    )
    if source_md.strip():
        shop_row = set_shop_source_md(shop_pk, source_md)
    return {
        "ok": True,
        "shop": shop_row,
        "upserted": len(upserted),
        "deleted_clean": int(deleted_clean or 0),
        "source_md_saved": bool(source_md.strip()),
        "bad_goods_sum": int(shop_row.get("bad_goods_sum") or 0),
        "goods_sum": int(shop_row.get("goods_sum") or 0),
    }


def list_shop_goods_api(owner_user_id: int, tb_shop_id: str) -> dict:
    tb_shop_id = str(tb_shop_id or "").strip()
    if not tb_shop_id:
        raise ValueError("shop_id 不能为空")
    shop = get_shop(int(owner_user_id), tb_shop_id)
    if not shop:
        raise ValueError("店铺不存在")
    goods = list_goods(int(shop["id"]))
    enriched = []
    for g in goods:
        row = dict(g)
        row.update(parse_problem_display(g.get("problem") or ""))
        enriched.append(row)
    return {"ok": True, "shop": shop, "goods": enriched, "count": len(enriched)}


_NOISE_CTX_RE = re.compile(
    r"立即领取|最高立减|满减券|优惠券|补贴|币淘|领券|天猫币|淘金币|折上折"
)


def _is_noise_context(ctx: str) -> bool:
    """优惠条幅/领券类 OCR 噪声，不当作摘要展示。"""
    s = (ctx or "").strip()
    if not s:
        return True
    if _NOISE_CTX_RE.search(s):
        return True
    # 过短且无实质（纯符号）
    if len(re.sub(r"[\W_]+", "", s, flags=re.U)) < 2:
        return True
    return False


def _source_label(src: str) -> str:
    s = (src or "").strip()
    if s in ("主图文字", "标题"):
        return "主图"
    if s in ("详情文本", "详情图文字"):
        return "详情页"
    return s or ""


def _llm_true_keywords_from_problem(text: str) -> set[str] | None:
    """若 problem 含可解析的 LLM JSON，返回 violate=true 的关键词集合；否则 None。"""
    jm = re.search(r"```json\s*(\{.*?\})\s*```", text or "", re.S)
    if not jm:
        return None
    try:
        obj = json.loads(jm.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "violations" not in obj:
        return None
    viols = obj.get("violations")
    if not isinstance(viols, list):
        return None
    true_kws: set[str] = set()
    for v in viols:
        if not isinstance(v, dict):
            continue
        val = v.get("violate")
        ok = val is True or val == 1
        if isinstance(val, str) and val.strip().lower() in ("true", "1", "yes"):
            ok = True
        if not ok:
            continue
        kw = str(v.get("keyword") or "").strip()
        if kw:
            true_kws.add(kw)
    return true_kws


def parse_problem_display(problem: str) -> dict:
    """把 goods_tb.problem 解析成网页表格用的命中词 + 摘要（含 LLM 判定）。

    摘要只保留词表命中上下文（主图/详情页：…），不写 LLM 长篇理由，
    也不展示优惠条幅类噪声上下文。
    若有 LLM JSON：只展示 violate=true；全为假阳性则 has_problem=false。
    """
    text = problem or ""
    true_kws = _llm_true_keywords_from_problem(text)
    keywords: list[str] = []
    summaries: list[str] = []
    for m in re.finditer(
        r"命中「([^」]+)」[：:]?\s*(.*)$",
        text,
        re.M,
    ):
        kw = m.group(1).strip()
        ctx = (m.group(2) or "").strip()
        if true_kws is not None and kw not in true_kws:
            continue
        if kw and kw not in keywords:
            keywords.append(kw)
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        src = ""
        sm = re.search(r"\[([^/\]]+)/([^\]]+)\]", line)
        if sm:
            src = sm.group(2).strip()
        if _is_noise_context(ctx):
            continue
        label_src = _source_label(src)
        label = (label_src + "：" if label_src else "") + (ctx or f"命中「{kw}」")
        if len(label) > 80:
            label = label[:77] + "…"
        if label and label not in summaries:
            summaries.append(label)

    # LLM 块：只并入 violate=true 的命中词（无词表行时也能显示）
    if true_kws is not None:
        for kw in sorted(true_kws):
            if kw not in keywords:
                keywords.append(kw)
        has_problem = bool(keywords or summaries)
    else:
        has_problem = bool(keywords or summaries or text.strip())

    return {
        "hit_keywords": keywords,
        "hit_summary": "\n".join(summaries),
        "has_problem": has_problem,
    }


def upload_shop_uniq_api(payload: dict) -> dict:
    """写入 shop_uniq_tb（平台自有总表）。

    客户端默认不再调用；以后由后台从用户库提取汇聚。
    内容 hash 相同则不动。
    """
    if not isinstance(payload, dict):
        raise ValueError("body 必须是对象")
    name = str(payload.get("shop_name") or "").strip()
    link = str(payload.get("shop_link") or "").strip()
    content = payload.get("shop_content")
    if content is None:
        content = payload.get("content")
    if content is None:
        raise ValueError("缺少 shop_content")
    content = str(content)
    return upsert_shop_uniq(name, link, content)



def import_goods_from_client(payload: dict, owner_user_id: int) -> dict:
    """接收本机客户端上传的商品详情，写入 goods.md，可选 OCR。"""
    if not owner_user_id:
        raise ValueError("未登录，无法导入商品")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items 不能为空")
    replace = bool(payload.get("replace", True))
    do_ocr = bool(payload.get("ocr", True))
    cfg = _read_config()
    main_ocr_count = cfg.getint("image", "main_ocr_count", fallback=2)
    detail_ocr_count = cfg.getint("image", "detail_ocr_count", fallback=6)

    goods = {} if replace else load_goods()
    ocr_done = 0
    ok_n = 0
    titles: dict[str, str] = {}
    ids: list[str] = []

    for n, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "").strip()
        if not iid:
            continue
        ids.append(iid)
        title = str(it.get("title") or iid).strip() or iid
        title = re.sub(r"^\d+\.\s*", "", title).strip()
        if title and title != iid:
            titles[iid] = title
        detail_texts = it.get("detail_texts") or []
        if isinstance(detail_texts, str):
            detail_text = detail_texts.strip()
        else:
            detail_text = "\n".join(str(x) for x in detail_texts if str(x).strip())
        main_ocr = [str(x) for x in (it.get("main_ocr") or []) if str(x).strip()]
        detail_ocr = [str(x) for x in (it.get("detail_ocr") or []) if str(x).strip()]

        if do_ocr:
            if not main_ocr:
                for u in (it.get("main_image_urls") or [])[:main_ocr_count]:
                    lines = ocr_image_url(str(u))
                    if lines:
                        main_ocr.append(" ".join(lines))
                        ocr_done += 1
            if not detail_ocr:
                for u in (it.get("detail_image_urls") or [])[:detail_ocr_count]:
                    lines = ocr_image_url(str(u))
                    if lines:
                        detail_ocr.append(" ".join(lines))
                        ocr_done += 1

        usable = (title and title != iid) or main_ocr or detail_text or detail_ocr
        if usable:
            ok_n += 1
        goods[iid] = {
            "id": iid,
            "index": n,
            "title": title,
            "main_ocr": main_ocr,
            "detail_text": detail_text,
            "detail_ocr": detail_ocr,
        }

    if not goods:
        raise ValueError("没有可写入的商品")

    path = save_goods(goods)

    shop = payload.get("shop") or {}
    if isinstance(shop, dict) and shop.get("shop_id") and shop.get("user_id"):
        upsert_shop(
            int(owner_user_id),
            str(shop["shop_id"]), str(shop["user_id"]),
            shop_name=str(shop.get("shop_name") or ""),
            sample_item_id=ids[0] if ids else "",
            item_ids=ids if len(ids) >= 20 else None,
            item_titles=titles or None,
        )

    return {
        "ok": True,
        "count": len(goods),
        "usable": ok_n,
        "ocr_done": ocr_done,
        "goods_file": str(path),
        "client": payload.get("client") or "",
        "client_version": payload.get("client_version") or "",
    }


def _shops_path() -> Path:
    cfg = _read_config()
    rel = cfg.get("scan", "shops_file", fallback="file/shops.json").strip()
    return ROOT / rel


def load_shops(owner_user_id: int) -> list[dict]:
    return db_list_shops(int(owner_user_id))


def upsert_shop(
    owner_user_id: int,
    shop_id: str,
    seller_id: str,
    shop_name: str = "",
    shop_url: str = "",
    sample_item_id: str = "",
    item_ids: list[str] | None = None,
    item_titles: dict[str, str] | None = None,
) -> dict:
    shop_id = str(shop_id).strip()
    seller_id = str(seller_id).strip()
    if not owner_user_id:
        raise ValueError("owner_user_id 无效")
    if not shop_id or not seller_id:
        raise ValueError("shop_id / seller_id 不能为空")
    cleaned: list[str] | None = None
    if item_ids is not None:
        cleaned = []
        for x in item_ids:
            xs = str(x).strip()
            if xs and xs not in cleaned:
                cleaned.append(xs)
        old = db_get_shop(int(owner_user_id), shop_id)
        old_n = len((old or {}).get("item_ids") or [])
        # 禁止用 CDN 首页那种个位数列表覆盖/冒充全店缓存
        if not (len(cleaned) >= 20 and len(cleaned) >= old_n):
            cleaned = None
    titles = None
    if item_titles:
        titles = {
            str(k).strip(): str(v).strip()
            for k, v in item_titles.items()
            if str(k).strip() and str(v).strip()
        }
    url = (shop_url or "").strip()
    if sample_item_id and not url:
        url = f"https://item.taobao.com/item.htm?id={sample_item_id}"
    return upsert_shop_db(
        owner_user_id=int(owner_user_id),
        shop_id=shop_id,
        seller_id=seller_id,
        shop_name=shop_name or "",
        shop_url=url,
        item_ids=cleaned,
        item_titles=titles,
    )


def get_shop(owner_user_id: int, shop_id: str) -> dict | None:
    return db_get_shop(int(owner_user_id), str(shop_id or "").strip())


def delete_shop(owner_user_id: int, shop_id: str) -> dict:
    res = delete_shop_db(int(owner_user_id), str(shop_id or "").strip())
    shops = load_shops(int(owner_user_id))
    return {"ok": True, "deleted": res.get("deleted", 0), "count": len(shops)}


def shops_status(owner_user_id: int) -> dict:
    shops = load_shops(int(owner_user_id))
    return {
        "ok": True,
        "source": "absolute_term_db.shop_tb",
        "count": len(shops),
        "shops": shops,
    }


def add_shop_from_url(url: str, owner_user_id: int) -> dict:
    """仅解析商品链接并写入店铺列表，不抓全店、不扫描。"""
    from fetch_shop import resolve_shop_info

    url = (url or "").strip()
    if not url:
        raise ValueError("请粘贴商品链接")
    if not owner_user_id:
        raise ValueError("未登录，无法写入店铺")
    item_ids = _extract_item_ids(url)
    if not item_ids:
        raise ValueError("链接里没有商品 ID，请粘贴商品页链接（含 id=）")
    cookie = load_cookie(user_id=int(owner_user_id))
    if not cookie:
        raise ValueError("未配置 Cookie，无法解析店铺（请先在网页或客户端保存 Cookie）")
    info = resolve_shop_info(item_ids[0], cookie=cookie)
    if "error" in info:
        raise ValueError(info["error"])
    shop = upsert_shop(
        int(owner_user_id),
        info["shop_id"], info["user_id"],
        shop_name=info.get("shop_name") or "",
        sample_item_id=item_ids[0],
    )
    return {"ok": True, "shop": shop, "all_item_count": info.get("all_item_count", 0)}


def migrate_shops_json_to_db(owner_user_id: int) -> dict:
    """一次性把旧 file/shops.json 迁入 shop_tb。"""
    path = _shops_path()
    if not path.is_file():
        return {"ok": True, "migrated": 0, "skipped": True, "reason": "无 shops.json"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"shops.json 无效: {e}") from e
    shops = data.get("shops") if isinstance(data, dict) else data
    if not isinstance(shops, list):
        raise ValueError("shops.json 格式错误：缺少 shops 数组")
    n = 0
    for s in shops:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("shop_id") or "").strip()
        uid = str(s.get("user_id") or "").strip()
        if not sid or not uid:
            continue
        upsert_shop(
            int(owner_user_id),
            sid,
            uid,
            shop_name=str(s.get("shop_name") or ""),
            sample_item_id=str(s.get("sample_item_id") or ""),
            item_ids=list(s.get("item_ids") or []),
            item_titles=dict(s.get("item_titles") or {}),
        )
        n += 1
    bak = path.with_suffix(path.suffix + ".migrated")
    path.replace(bak)
    return {"ok": True, "migrated": n, "backup": str(bak.relative_to(ROOT))}


def create_task(url: str, ocr: bool, llm: bool, max_items: int, max_pages: int,
                force_rescan: bool = False, shop_id: str = "",
                owner_user_id: int = 0) -> str:
    """创建后台扫描任务，立即返回 task_id。"""
    import uuid
    if not owner_user_id:
        raise ValueError("未登录，无法创建扫描任务")
    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id, "url": url, "shop_id": shop_id, "status": "running",
        "current": 0, "total": 0, "current_title": "",
        "notice": "", "error": "", "results": [],
        "force_rescan": force_rescan,
        "owner_user_id": int(owner_user_id),
    }
    with _TASK_LOCK:
        TASKS[task_id] = task

    def _run() -> None:
        try:
            result = scan_shop(
                url, ocr=ocr, llm=llm, max_items=max_items,
                max_pages=max_pages, force_rescan=force_rescan,
                shop_id=shop_id, progress_cb=_on_progress,
                owner_user_id=int(owner_user_id),
            )
            with _TASK_LOCK:
                task.update({
                    "status": "done",
                    "total": result["total"],
                    "notice": result["notice"],
                    "mode": result.get("mode", "items"),
                    "results": result["results"],
                    "cached": result.get("cached", 0),
                    "fetched": result.get("fetched", 0),
                    "goods_file": result.get("goods_file", ""),
                })
        except Exception as e:  # noqa: BLE001
            with _TASK_LOCK:
                task.update({"status": "error", "error": str(e)})

    def _on_progress(current: int, total: int, title: str) -> None:
        with _TASK_LOCK:
            task.update({"current": current, "total": total, "current_title": title})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return task_id


def get_task(task_id: str) -> dict | None:
    with _TASK_LOCK:
        task = TASKS.get(task_id)
        return dict(task) if task else None


def _extract_item_ids(url: str) -> list[str]:
    """从粘贴文本中提取淘宝/天猫商品 ID。

    兼容:
      - https://item.taobao.com/item.htm?abbucket=4&id=663624064367
      - https://detail.tmall.com/item.htm?id=xxx
      - https://a.m.taobao.com/i663624064367.htm
      - 纯数字 ID / 多条逗号换行分隔
    """
    text = (url or "").strip()
    if not text:
        return []
    ids: list[str] = []

    def _add(iid: str) -> None:
        if iid and iid not in ids:
            ids.append(iid)

    # id 可在任意 query 位置（?id= / &id=）
    for m in re.finditer(r"[?&]id=(\d{5,})", text, re.I):
        _add(m.group(1))
    for m in re.finditer(r"(?:a\.m\.taobao\.com)/i(\d{5,})\.htm", text, re.I):
        _add(m.group(1))
    if ids:
        return ids

    # 多条：逗号/分号/换行分隔（不用空白切整段 URL，避免拆坏 query）
    for c in re.split(r"[,，;；\n]+", text):
        c = c.strip()
        if not c:
            continue
        m = re.search(r"[?&]id=(\d{5,})", c, re.I)
        if m:
            _add(m.group(1))
            continue
        m = re.search(r"/i(\d{5,})\.htm", c, re.I)
        if m:
            _add(m.group(1))
            continue
        if re.fullmatch(r"\d{4,}", c):
            _add(c)
    return ids


def _judge_item(
    iid: str,
    title: str,
    main_ocr: list[str],
    detail_text: str,
    word_groups: list[tuple[str, list[str]]],
    llm: bool,
    detail_ocr: list[str] | None = None,
    index: int = 0,
) -> dict:
    """对单条商品资料做词表 + 可选 LLM 判定。"""
    from scanner import llm_judge, scan_texts

    detail_ocr = detail_ocr or []
    texts: dict[str, str] = {"标题": title}
    if detail_text:
        texts["详情文本"] = detail_text
    if main_ocr:
        texts["主图文字"] = "\n".join(main_ocr)
    if detail_ocr:
        texts["详情图文字"] = "\n".join(detail_ocr)

    hits: list[dict] = []
    for category, words in word_groups:
        if words:
            hits.extend(scan_texts(words, texts, category=category))
    seen: set[tuple] = set()
    uniq_hits: list[dict] = []
    for h in hits:
        k = (h.get("category", ""), h["source"], h["keyword"])
        if k not in seen:
            seen.add(k)
            uniq_hits.append(h)
    judge = ""
    if llm and uniq_hits:
        it = {"id": iid, "title": title, "url": f"https://item.taobao.com/item.htm?id={iid}"}
        judge = llm_judge(it, uniq_hits)
        print(f"  LLM: {judge[:120]}", flush=True)
    return {
        "id": iid,
        "index": index,
        "title": title,
        "url": f"https://item.taobao.com/item.htm?id={iid}",
        "hits": uniq_hits,
        "ocr_text": "\n".join(main_ocr)[:300],
        "judge": judge,
    }


def scan_from_goods(url: str = "", llm: bool = True, max_items: int = 0, progress_cb=None) -> dict:
    """只从 file/goods.md 读取资料做词表/LLM 扫描，不访问淘宝。

    开始扫描始终扫 goods.md 全部（可受 max_items 限制），不吃输入框里的淘宝链接，
    避免「链接还留着时只扫 1 个商品、命中突然变少」。
    """
    cached_goods = load_goods()
    if not cached_goods:
        raise ValueError("goods.md 为空，请先点「重新扫描」抓取并保存商品资料")

    # 按序号排序，保证清单稳定
    ordered = sorted(
        cached_goods.items(),
        key=lambda kv: int((kv[1] or {}).get("index") or 0) or 10**9,
    )
    item_ids = [iid for iid, _ in ordered]
    notice = f"从 goods.md 读取全部 {len(item_ids)} 个商品（忽略输入框链接）"

    if max_items > 0:
        item_ids = item_ids[:max_items]
        notice = f"从 goods.md 读取前 {len(item_ids)} 个商品（上限 {max_items}）"

    word_groups = load_word_groups()
    results: list[dict] = []
    total = len(item_ids)
    for n, iid in enumerate(item_ids, 1):
        g = cached_goods[iid]
        title = g.get("title") or iid
        main_ocr = list(g.get("main_ocr") or [])
        detail_text = g.get("detail_text") or ""
        detail_ocr = list(g.get("detail_ocr") or [])
        idx = int(g.get("index") or n)
        print(f"[goods-scan] [{idx}/{total}] {iid} {title[:40]}", flush=True)
        if progress_cb:
            progress_cb(n, total, f"{idx}. {title}")
        row = _judge_item(
            iid, title, main_ocr, detail_text, word_groups, llm=llm,
            detail_ocr=detail_ocr, index=idx,
        )
        row["error"] = ""
        row["from_cache"] = True
        results.append(row)

    goods_path = _goods_path()
    return {
        "shop": url or "goods.md",
        "shop_key": "",
        "mode": "goods",
        "total": total,
        "scanned": len(results),
        "notice": notice + f"；未访问淘宝，资料来自 {goods_path.name}",
        "results": results,
        "cached": len(results),
        "fetched": 0,
        "goods_file": str(goods_path),
    }


def scan_shop(url: str, ocr: bool = True, llm: bool = True, max_items: int = 0, max_pages: int = 5,
              force_rescan: bool = False, shop_id: str = "", progress_cb=None,
              owner_user_id: int = 0) -> dict:
    """扫描入口。

    - 不重新扫描：只从 file/goods.md 调用资料做检测
    - 重新扫描(force_rescan)：抓淘宝 → 写入 goods.md → 再检测
      可用已保存 shop_id（下拉选店），或粘贴商品/店铺链接（首次会写入店铺列表）
    """
    if not owner_user_id:
        raise ValueError("未登录，无法扫描")
    if not force_rescan:
        return scan_from_goods(url=url, llm=llm, max_items=max_items, progress_cb=progress_cb)

    import requests  # noqa: F401
    from fetch_item import _session, fetch_item
    from fetch_shop import fetch_shop_catalog, fetch_shop_items, parse_shop_key, resolve_shop_info

    cfg = _read_config()
    word_groups = load_word_groups()
    cookie = load_cookie(user_id=int(owner_user_id))
    main_ocr_count = cfg.getint("image", "main_ocr_count", fallback=2)
    detail_ocr_count = cfg.getint("image", "detail_ocr_count", fallback=6)
    item_delay = cfg.getfloat("scan", "item_delay_seconds", fallback=1.8)
    wind_pause_after = cfg.getint("scan", "wind_control_pause_after", fallback=3)
    wind_pause_sec = cfg.getfloat("scan", "wind_control_pause_seconds", fallback=45)

    item_ids: list[str] = []
    catalog_titles: dict[str, str] = {}
    shop_key = ""
    shop_total = 0
    notice = ""
    mode = "items"
    url = (url or "").strip()
    shop_id = (shop_id or "").strip()
    owner_id = int(owner_user_id)

    def _ingest_catalog(res: dict) -> tuple[list[str], dict[str, str]]:
        ids: list[str] = []
        titles: dict[str, str] = {}
        for it in res.get("items") or []:
            iid = str(it.get("id") or "").strip()
            if not iid:
                continue
            ids.append(iid)
            t = str(it.get("title") or "").strip()
            if t:
                titles[iid] = t
        return ids, titles

    if shop_id:
        # 下拉选店：直接用已保存的 shop_id/user_id，不必再贴商品链接
        saved = get_shop(owner_id, shop_id)
        if not saved:
            raise ValueError("店铺不在列表中，请先用商品链接添加一次")
        mode = "shop"
        shop_name = saved.get("shop_name") or shop_id
        try:
            res = fetch_shop_catalog(
                saved["shop_id"], saved["user_id"], cookie=cookie, timeout=20,
                cached_item_ids=list(saved.get("item_ids") or []),
                cached_item_titles=dict(saved.get("item_titles") or {}),
            )
            item_ids, catalog_titles = _ingest_catalog(res)
            notice = f"已选店铺「{shop_name}」, {res.get('notice') or f'获取到 {len(item_ids)} 个商品'}"
            persist_ids = (
                item_ids
                if len(item_ids) >= 20
                and not res.get("from_cache_ids")
                and "CDN" not in (res.get("notice") or "")
                else None
            )
            upsert_shop(
                owner_id,
                saved["shop_id"], saved["user_id"],
                shop_name=shop_name,
                sample_item_id=item_ids[0] if item_ids else "",
                item_ids=persist_ids,
                item_titles=catalog_titles or None,
            )
            if max_items > 0:
                item_ids = item_ids[:max_items]
            shop_total = len(item_ids)
        except (ValueError, requests.RequestException) as e:
            raise ValueError(f"店铺「{shop_name}」商品列表获取失败: {e}") from e
    elif not url:
        raise ValueError("重新扫描请选择已保存店铺，或粘贴商品/店铺链接")
    else:
        item_ids = _extract_item_ids(url)

        if not item_ids:
            mode = "shop"
            shop_key = parse_shop_key(url)
            res = fetch_shop_items(shop_key, cookie=cookie, max_pages=max_pages)
            item_ids, catalog_titles = _ingest_catalog(res)
            shop_total = res["total"]
            notice = res["notice"]
            if max_items > 0:
                item_ids = item_ids[:max_items]
        elif len(item_ids) == 1:
            mode = "shop"
            sample_id = item_ids[0]
            info = resolve_shop_info(sample_id, cookie=cookie)
            if "error" not in info:
                shop_name = info.get("shop_name", "")
                # 解析成功立刻入库（不必等 CDN/整店扫完），下拉列表马上可用
                upsert_shop(
                    owner_id,
                    info["shop_id"], info["user_id"],
                    shop_name=shop_name,
                    sample_item_id=sample_id,
                )
                if progress_cb:
                    progress_cb(0, 0, f"已保存店铺「{shop_name}」")
                try:
                    saved0 = get_shop(owner_id, info["shop_id"]) or {}
                    res = fetch_shop_catalog(
                        info["shop_id"], info["user_id"], cookie=cookie, timeout=20,
                        cached_item_ids=list(saved0.get("item_ids") or []),
                        cached_item_titles=dict(saved0.get("item_titles") or {}),
                    )
                    item_ids, catalog_titles = _ingest_catalog(res)
                    # 保证入口商品一定在列表里
                    if sample_id not in item_ids:
                        item_ids.insert(0, sample_id)
                    notice = (
                        f"自动解析到店铺「{shop_name}」, "
                        + (res.get("notice") or f"获取到 {len(item_ids)} 个商品")
                    )
                    persist_ids = (
                        item_ids
                        if len(item_ids) >= 20
                        and not res.get("from_cache_ids")
                        and "CDN" not in (res.get("notice") or "")
                        else None
                    )
                    upsert_shop(
                        owner_id,
                        info["shop_id"], info["user_id"],
                        shop_name=shop_name,
                        sample_item_id=sample_id,
                        item_ids=persist_ids,
                        item_titles=catalog_titles or None,
                    )
                    if max_items > 0:
                        item_ids = item_ids[:max_items]
                    shop_total = len(item_ids)
                except (ValueError, requests.RequestException) as e:
                    notice = f"店铺「{shop_name}」已保存；全店列表失败({e}), 仅扫描该商品"
            else:
                notice = f"店铺解析失败({info['error']}), 仅扫描该商品"
        else:
            item_ids = list(dict.fromkeys(item_ids))
            if max_items > 0:
                item_ids = item_ids[:max_items]
            notice = f"商品链接模式: {len(item_ids)} 个商品"

    if not item_ids:
        raise ValueError(notice or "未解析到任何商品")

    # 全店重扫：goods.md 只保留本次店铺商品，避免混进旧店缓存
    replace_goods = mode == "shop"
    cached_goods = {} if replace_goods else load_goods()
    results: list[dict] = []
    total = shop_total or len(item_ids)
    fetched_n = 0
    wind_streak = 0
    title_only_mode = False
    title_only_n = 0
    detail_ok_n = 0
    sess = _session()

    for n, iid in enumerate(item_ids, 1):
        print(f"[shop-scan] [{n}/{len(item_ids)}] {iid}", flush=True)
        g0 = cached_goods.get(iid) or {}
        cache_usable = bool(
            g0 and (
                (g0.get("title") and g0.get("title") != iid)
                or g0.get("main_ocr")
                or g0.get("detail_text")
                or g0.get("detail_ocr")
            )
        )
        main_ocr: list[str] = []
        detail_ocr: list[str] = []
        detail_text = ""
        error = ""
        title = catalog_titles.get(iid) or iid
        degraded = False

        if title_only_mode:
            # 连续风控后降级：不再打详情接口，用列表标题扫极限词
            error = "详情风控降级：仅用店铺列表标题扫描"
            degraded = True
            title_only_n += 1
        else:
            detail = fetch_item(iid, session=sess)
            title = detail.get("title") or catalog_titles.get(iid) or iid
            error = detail.get("error", "")
            if detail.get("detail_texts"):
                detail_text = "\n".join(detail["detail_texts"])
            wind = bool(detail.get("wind_control"))
            if wind:
                wind_streak += 1
                if wind_streak >= wind_pause_after:
                    print(
                        f"[shop-scan] 连续风控 {wind_streak} 次，暂停 {wind_pause_sec:.0f}s",
                        flush=True,
                    )
                    if progress_cb:
                        progress_cb(n, total, f"淘宝风控，暂停 {wind_pause_sec:.0f}s…")
                    time.sleep(wind_pause_sec)
                    # 暂停后再试当前商品一次；仍风控则切标题模式
                    detail2 = fetch_item(iid, session=sess)
                    if detail2.get("wind_control"):
                        title_only_mode = True
                        title = detail2.get("title") or catalog_titles.get(iid) or title
                        error = (detail2.get("error") or error) + " | 后续改用列表标题扫描"
                        degraded = True
                        title_only_n += 1
                        wind_streak = 0
                    else:
                        detail = detail2
                        title = detail.get("title") or catalog_titles.get(iid) or title
                        error = detail.get("error", "")
                        detail_text = "\n".join(detail.get("detail_texts") or [])
                        wind_streak = 0
                        wind = False
            else:
                wind_streak = 0

            if not wind and not degraded:
                if ocr:
                    for u in (detail.get("main_image_urls") or [])[:main_ocr_count]:
                        lines = ocr_image_url(u)
                        if lines:
                            main_ocr.append(" ".join(lines))
                    for u in (detail.get("detail_image_urls") or [])[:detail_ocr_count]:
                        lines = ocr_image_url(u)
                        if lines:
                            detail_ocr.append(" ".join(lines))
                if (title and title != iid) or detail_text or main_ocr:
                    detail_ok_n += 1

        fetched_n += 1
        new_usable = (title and title != iid) or main_ocr or detail_text or detail_ocr
        if new_usable or not cache_usable:
            cached_goods[iid] = {
                "id": iid,
                "index": n,
                "title": title,
                "main_ocr": main_ocr,
                "detail_text": detail_text,
                "detail_ocr": detail_ocr,
            }
        else:
            title = g0.get("title") or title
            main_ocr = list(g0.get("main_ocr") or [])
            detail_text = g0.get("detail_text") or ""
            detail_ocr = list(g0.get("detail_ocr") or [])
            cached_goods[iid]["index"] = n

        if progress_cb:
            tag = "（标题降级）" if degraded or title_only_mode else ""
            progress_cb(n, total, f"{n}. {title}{tag}")

        row = _judge_item(
            iid, title, main_ocr, detail_text, word_groups, llm=llm,
            detail_ocr=detail_ocr, index=n,
        )
        row["error"] = error
        row["from_cache"] = False
        row["degraded"] = degraded or title_only_mode
        results.append(row)

        if n < len(item_ids) and not title_only_mode and item_delay > 0:
            time.sleep(item_delay)

    # 全量重扫后按扫描顺序重新编号
    for i, iid in enumerate(item_ids, 1):
        if iid in cached_goods:
            cached_goods[iid]["index"] = i
    goods_path = save_goods(cached_goods)
    notice = (notice + "；" if notice else "") + (
        f"重新扫描完成，新抓取 {fetched_n}（详情成功 {detail_ok_n}"
        + (f"，标题降级 {title_only_n}" if title_only_n else "")
        + f"），已写入 {goods_path.name}"
    )
    if title_only_n:
        notice += "。详情被淘宝风控时已用店铺列表标题继续扫极限词；详情图文需换 Cookie 后重试"

    return {
        "shop": url,
        "shop_key": shop_key,
        "mode": mode,
        "total": shop_total or len(item_ids),
        "scanned": len(results),
        "notice": notice,
        "results": results,
        "cached": 0,
        "fetched": fetched_n,
        "goods_file": str(goods_path),
    }


class Handler(BaseHTTPRequestHandler):
    def address_string(self) -> str:
        addr = self.client_address
        if isinstance(addr, tuple) and addr:
            return str(addr[0])
        return "unix"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[absolute] {self.address_string()} {fmt % args}", flush=True)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, obj: dict | list) -> None:
        raw = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_UPLOAD + 1024 * 1024:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def _client_ip(self) -> str:
        """取真实客户端 IP：nginx 的 X-Forwarded-For 首个，其次 X-Real-IP；没有就空。"""
        xff = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if xff:
            return xff
        return (self.headers.get("X-Real-IP") or "").strip()

    def _record_login_ip(self, user_id) -> None:
        """登录成功后记最近登录 IP/城市；拿不到 IP 就不记（不猜）。"""
        try:
            record_login_async(int(user_id), self._client_ip())
        except Exception as e:  # noqa: BLE001
            print(f"[geo] 发起登录记录失败: {e}", file=sys.stderr)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        path = self._path()
        try:
            if path in ("/api/health", "/health"):
                db_info = {}
                try:
                    db_info = health_db()
                except DbError as e:
                    db_info = {"ok": False, "error": str(e)}
                self._json(HTTPStatus.OK, {"ok": True, "db": db_info})
                return
            if path in ("/api/me", "/me"):
                user = require_user(self)
                full = get_user_by_id(int(user["id"])) or user
                self._json(HTTPStatus.OK, public_me(full))
                return
            if path in ("/api/scan-shops", "/scan-shops"):
                user = require_user(self)
                shops = list_scan_shops(int(user["id"]))
                self._json(HTTPStatus.OK, {"ok": True, "shops": shops, "count": len(shops)})
                return
            if path in ("/api/settings", "/settings"):
                require_admin(self)
                self._json(HTTPStatus.OK, public_settings())
                return
            if path in ("/api/scan", "/scan"):
                require_user(self)
                out_path = _read_config().get("scan", "output_file", fallback="output/违规清单.txt")
                report = (ROOT / out_path).read_text(encoding="utf-8")
                self._text(HTTPStatus.OK, report, "text/plain; charset=utf-8")
                return
            if path in ("/api/config", "/config"):
                require_user(self)
                report = CONFIG_PATH.read_text(encoding="utf-8")
                self._text(HTTPStatus.OK, report, "text/plain; charset=utf-8")
                return
            if path in ("/api/ui", "/ui"):
                cfg = _read_config()
                def _g(section: str, key: str, fallback: str = "") -> str:
                    return (cfg.get(section, key, fallback=fallback) or "").strip()

                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "houtai_url": _g("ui", "houtai_url", "/houtai/"),
                    "home_url": _g("ui", "home_url", "https://www.imocfood.com"),
                    "public_base": _g("ui", "public_base", "/absolute_term"),
                    "recharge_url": _g("ui", "recharge_url"),
                    "cookie_guide_url": _g("ui", "cookie_guide_url"),
                    "register_url": _g("ui", "register_url"),
                    "db_admin_url": _g("ui", "db_admin_url") or (
                        (_g("ui", "public_base", "/absolute_term").rstrip("/") + "/db.html")
                    ),
                    "link_harvest_url": _g("client", "link_harvest_url"),
                    "link_harvest_search_url": _g("client", "link_harvest_search_url"),
                    "link_harvest_keyword": _g("client", "link_harvest_keyword"),
                    "link_harvest_count": _g("client", "link_harvest_count"),
                    "link_harvest_max_try": _g("client", "link_harvest_max_try"),
                    "link_harvest_wait_seconds": _g("client", "link_harvest_wait_seconds"),
                    "auto_random_max_shops": _g("client", "auto_random_max_shops"),
                    "auto_random_pause_seconds": _g("client", "auto_random_pause_seconds"),
                    "auto_random_popup_problems": _g("client", "auto_random_popup_problems"),
                })
                return
            if path in ("/api/llm", "/llm"):
                require_user(self)
                conf = load_llm_config(config_path=CONFIG_PATH)
                key = conf.get("api_key") or ""
                masked = "" if not key else (key if len(key) <= 8 else (key[:4] + "…" + key[-4:]))
                self._json(HTTPStatus.OK, {
                    "api_url": conf.get("api_url", ""),
                    "model": conf.get("model", ""),
                    "api_key_masked": masked,
                    "has_key": bool(key),
                })
                return
            if path in ("/api/cookie", "/cookie"):
                require_user(self)
                self._json(HTTPStatus.OK, cookie_status())
                return
            # 扫描任务进度
            if path in ("/api/scan/status", "/scan/status"):
                require_user(self)
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                task_id = (q.get("task_id") or [""])[0]
                task = get_task(task_id) if task_id else None
                if not task:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
                    return
                self._json(HTTPStatus.OK, task)
                return
            # 双词表状态 / 原文（在线编辑）
            if path in ("/api/files", "/files"):
                require_user(self)
                self._json(HTTPStatus.OK, files_status())
                return
            if path in ("/api/files/limit", "/files/limit"):
                require_user(self)
                self._json(HTTPStatus.OK, read_word_file("limit"))
                return
            if path in ("/api/files/wrong", "/files/wrong"):
                require_user(self)
                self._json(HTTPStatus.OK, read_word_file("wrong"))
                return
            if path in ("/api/goods", "/goods"):
                require_user(self)
                self._json(HTTPStatus.OK, goods_status())
                return
            if path in ("/api/goods/migrate", "/goods/migrate"):
                require_user(self)
                self._json(HTTPStatus.OK, migrate_goods_split())
                return
            if path in ("/api/client/info", "/client/info"):
                self._json(HTTPStatus.OK, client_info())
                return
            if path in ("/api/client/app-update", "/client/app-update"):
                from urllib.parse import parse_qs, urlparse

                q = parse_qs(urlparse(self.path).query)
                current = (q.get("current") or [""])[0]
                self._json(HTTPStatus.OK, client_app_update_api(current))
                return
            if path in ("/api/shops", "/shops"):
                user = require_user(self)
                self._json(HTTPStatus.OK, shops_status(int(user["id"])))
                return
            if path in ("/api/client/bundle", "/client/bundle"):
                require_user(self)
                self._json(HTTPStatus.OK, client_scan_bundle())
                return
            if path in ("/api/llm/prompt", "/llm/prompt"):
                user = require_user(self)
                self._json(HTTPStatus.OK, get_llm_prompt_api(user))
                return
            if path in ("/api/llm/filter-prompt", "/llm/filter-prompt"):
                user = require_user(self)
                self._json(HTTPStatus.OK, get_filter_prompt_api(user))
                return
            if path in ("/api/goods/list", "/goods/list"):
                from urllib.parse import parse_qs, urlparse

                user = require_user(self)
                q = parse_qs(urlparse(self.path).query)
                shop_id = (q.get("shop_id") or [""])[0]
                self._json(HTTPStatus.OK, list_shop_goods_api(int(user["id"]), shop_id))
                return
            if path in ("/api/shops/source", "/shops/source"):
                from urllib.parse import parse_qs, urlparse

                user = require_user(self)
                q = parse_qs(urlparse(self.path).query)
                shop_id = (q.get("shop_id") or [""])[0]
                self._json(HTTPStatus.OK, get_shop_source_md(int(user["id"]), shop_id))
                return
            if path in ("/api/admin/db/tables", "/admin/db/tables"):
                require_admin(self)
                self._json(HTTPStatus.OK, {"ok": True, "tables": admin_db_tables()})
                return
            if path in ("/api/suggestions", "/suggestions"):
                user = require_user(self)
                self._json(HTTPStatus.OK, list_suggestions_api(user))
                return
            if path in ("/api/admin/db/rows", "/admin/db/rows"):
                from urllib.parse import parse_qs, urlparse

                require_admin(self)
                q = parse_qs(urlparse(self.path).query)
                table = (q.get("table") or [""])[0]
                limit = (q.get("limit") or ["50"])[0]
                offset = (q.get("offset") or ["0"])[0]
                self._json(HTTPStatus.OK, admin_db_rows(table, limit, offset))
                return
            # 充值：二维码图片（公开，仅渲染传入 data）
            if path in ("/api/pay/qr.png", "/pay/qr.png"):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                data = (q.get("data") or [""])[0]
                png = make_qr_png(data)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(png)
                return
            # 充值：订单状态（轮询）
            if path.startswith("/api/pay/orders/") or path.startswith("/pay/orders/"):
                user = require_user(self)
                order_no = path.rsplit("/", 1)[-1]
                self._json(HTTPStatus.OK, pay_order_status_api(user, order_no))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": f"接口不存在: {path}"})
        except PermissionError as e:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(e)})
        except DbError as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except SmsError as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except ValueError as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except FileNotFoundError as e:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

    def do_POST(self) -> None:
        path = self._path()
        try:
            if path in ("/api/login", "/login"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                username = str(data.get("username") or "").strip()
                password = str(data.get("password") or "")
                if not username or not password:
                    raise ValueError("请填写用户名和密码")
                # 客户端显式 client=true：会话不过期、与网页分离；网页不传则 channel=web 有 TTL
                is_client = bool(data.get("client"))
                channel = "client" if is_client else "web"
                user = authenticate(username, password)
                token = create_session(user, channel=channel)
                full = get_user_by_id(int(user["id"])) or user
                self._record_login_ip(user["id"])
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "token": token,
                    "user": public_me(full),
                    "channel": channel,
                })
                return

            if path in ("/api/auth/register", "/auth/register"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                res = register_api(data)
                self._record_login_ip(((res.get("user") or {}).get("id")) or 0)
                self._json(HTTPStatus.OK, res)
                return

            if path in ("/api/auth/send-sms", "/auth/send-sms"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                sess_user = None
                try:
                    sess_user = require_user(self)
                except PermissionError:
                    sess_user = None
                self._json(HTTPStatus.OK, send_sms_api(data, sess_user))
                return

            if path in ("/api/auth/reset-password", "/auth/reset-password"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                res = reset_password_api(data)
                self._record_login_ip(((res.get("user") or {}).get("id")) or 0)
                self._json(HTTPStatus.OK, res)
                return

            if path in ("/api/auth/recover-usernames", "/auth/recover-usernames"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, recover_usernames_api(data))
                return

            if path in ("/api/auth/bind-phone", "/auth/bind-phone"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, bind_phone_api(user, data))
                return

            if path in ("/api/auth/rename", "/auth/rename"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                res = rename_user_api(user, data)
                self._json(HTTPStatus.OK, res)
                return

            if path in ("/api/auth/web-ticket", "/auth/web-ticket"):
                user = require_user(self)
                self._json(HTTPStatus.OK, issue_web_ticket(user))
                return

            if path in ("/api/auth/consume-ticket", "/auth/consume-ticket"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                res = consume_web_ticket(data)
                self._record_login_ip(((res.get("user") or {}).get("id")) or 0)
                self._json(HTTPStatus.OK, res)
                return

            if path in ("/api/logout", "/logout"):
                destroy_session(_bearer_token(self))
                self._json(HTTPStatus.OK, {"ok": True})
                return

            if path in ("/api/settings", "/settings"):
                require_admin(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, save_settings(data.get("updates") or []))
                return

            if path in ("/api/admin/scan-fee", "/admin/scan-fee"):
                require_admin(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                if "yuan" not in data:
                    raise ValueError("缺少 yuan")
                yuan = float(data.get("yuan"))
                if yuan < 0:
                    raise ValueError("不能为负")
                _set_ini_option("billing", "scan_fee_yuan", f"{yuan:.2f}")
                fees = _billing_fees()
                self._json(HTTPStatus.OK, {"ok": True, **fees})
                return

            if path in ("/api/admin/auto-scan-fee", "/admin/auto-scan-fee"):
                require_admin(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                if "yuan" not in data:
                    raise ValueError("缺少 yuan")
                yuan = float(data.get("yuan"))
                if yuan < 0:
                    raise ValueError("不能为负")
                _set_ini_option("billing", "auto_scan_fee_yuan", f"{yuan:.2f}")
                fees = _billing_fees()
                self._json(HTTPStatus.OK, {"ok": True, **fees})
                return

            if path in ("/api/admin/adjust", "/admin/adjust"):
                require_admin(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                username = str(data.get("username") or "").strip()
                if not username:
                    raise ValueError("请填用户名")
                if "amount_cents" in data and data.get("amount_cents") is not None:
                    cents = int(data.get("amount_cents"))
                    delta = cents / 100.0
                elif "yuan" in data and data.get("yuan") is not None:
                    delta = float(data.get("yuan"))
                else:
                    raise ValueError("缺少 amount_cents 或 yuan")
                if delta == 0:
                    raise ValueError("金额不能为 0")
                target = get_user_by_username(username)
                if not target:
                    raise ValueError(f"用户不存在: {username}")
                new_bal = change_balance(int(target["id"]), delta)
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "username": username,
                    "balance": new_bal,
                    "balance_cents": int(round(new_bal * 100)),
                })
                return

            if path in ("/api/auto-scan/enable", "/auto-scan/enable"):
                user = require_user(self)
                fees = _billing_fees()
                res = enable_auto_scan(int(user["id"]), fees["auto_scan_fee_yuan"])
                full = get_user_by_id(int(user["id"])) or user
                self._json(HTTPStatus.OK, {**public_me(full), **res})
                return

            if path in ("/api/scan-shops/add", "/scan-shops/add"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                items = data.get("shops") or data.get("items")
                if items is None:
                    items = [data]
                if not isinstance(items, list) or not items:
                    raise ValueError("shops 必须是非空数组")
                added = 0
                skipped = 0
                errors: list[str] = []
                out: list[dict] = []
                for it in items:
                    if not isinstance(it, dict):
                        errors.append("条目不是对象")
                        continue
                    try:
                        r = add_scan_shop(
                            int(user["id"]),
                            shop_name=str(it.get("shop_name") or ""),
                            shop_link=str(it.get("shop_link") or it.get("url") or ""),
                            tb_shop_id=str(it.get("tb_shop_id") or it.get("shop_id") or ""),
                            seller_id=str(it.get("seller_id") or it.get("user_id") or ""),
                            item_id=str(it.get("item_id") or ""),
                        )
                    except DbError as e:
                        errors.append(str(e))
                        continue
                    if r.get("added"):
                        added += 1
                        if r.get("shop"):
                            out.append(r["shop"])
                    else:
                        skipped += 1
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "added": added,
                    "skipped": skipped,
                    "errors": errors[:12],
                    "shops": out,
                })
                return

            if path in ("/api/scan-shops/delete", "/scan-shops/delete"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                pk = int(data.get("id") or 0)
                tb_id = str(data.get("tb_shop_id") or data.get("shop_id") or "")
                self._json(
                    HTTPStatus.OK,
                    delete_scan_shop(int(user["id"]), pk=pk, tb_shop_id=tb_id),
                )
                return

            if path in ("/api/shops/migrate", "/shops/migrate"):
                user = require_user(self)
                self._json(HTTPStatus.OK, migrate_shops_json_to_db(int(user["id"])))
                return

            # 触发扫描
            if path in ("/api/scan", "/scan"):
                require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                ocr = data.get("ocr", True)
                llm = data.get("llm", True)
                report = run_scan(ocr=ocr, llm=llm)
                self._text(HTTPStatus.OK, report, "text/plain; charset=utf-8")
                return

            # 店铺链接自动扫描（后台任务，立即返回 task_id）
            if path in ("/api/scan/shop", "/scan/shop"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                url = (data.get("url") or "").strip()
                shop_id = (data.get("shop_id") or "").strip()
                force_rescan = bool(data.get("force_rescan", False))
                if force_rescan and not url and not shop_id:
                    raise ValueError("重新扫描请选择已保存店铺，或粘贴商品/店铺链接")
                cfg = _read_config()
                default_max = cfg.getint("scan", "default_max_items", fallback=0)
                if "max_items" in data and data.get("max_items") is not None and str(data.get("max_items")) != "":
                    max_items = int(data.get("max_items") or 0)
                else:
                    max_items = default_max
                task_id = create_task(
                    url,
                    ocr=bool(data.get("ocr", True)),
                    llm=bool(data.get("llm", True)),
                    max_items=max_items,
                    max_pages=int(data.get("max_pages") or 5),
                    force_rescan=force_rescan,
                    shop_id=shop_id,
                    owner_user_id=int(user["id"]),
                )
                self._json(HTTPStatus.OK, {
                    "task_id": task_id,
                    "status": "running",
                    "force_rescan": force_rescan,
                    "shop_id": shop_id,
                })
                return

            # 从下拉列表删除店铺
            if path in ("/api/shops/delete", "/shops/delete"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, delete_shop(int(user["id"]), data.get("shop_id") or ""))
                return

            # 仅用商品链接解析店铺并加入列表（不整店扫描）
            if path in ("/api/shops/add", "/shops/add"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, add_shop_from_url(data.get("url") or "", int(user["id"])))
                return

            # 保存在线编辑词表
            if path in ("/api/files/limit", "/files/limit"):
                require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, save_word_file("limit", data.get("content", "")))
                return
            if path in ("/api/files/wrong", "/files/wrong"):
                require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, save_word_file("wrong", data.get("content", "")))
                return

            # 保存淘宝 Cookie
            if path in ("/api/cookie", "/cookie"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                use_llm = bool(data.get("use_llm", True))
                result = save_cookie(
                    data.get("cookie", ""),
                    use_llm=use_llm,
                    user_id=int(user["id"]),
                )
                status = HTTPStatus.OK if result.get("valid") else HTTPStatus.BAD_REQUEST
                self._json(status, result)
                return

            # 本机客户端上传商品资料（旧：只写 goods.md；新客户端改走 /scan/results）
            if path in ("/api/goods/import", "/goods/import"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, import_goods_from_client(data, int(user["id"])))
                return

            # 本机已扫完：只落库 shop_tb / goods_tb
            if path in ("/api/scan/results", "/scan/results"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, import_scan_results(data, int(user["id"])))
                return

            if path in ("/api/llm/prompt", "/llm/prompt"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, save_llm_prompt_api(user, data))
                return
            if path in ("/api/llm/filter-prompt", "/llm/filter-prompt"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, save_filter_prompt_api(user, data))
                return

            # 用户意见/建议
            if path in ("/api/suggestion", "/suggestion"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, add_suggestion_api(user, data))
                return
            if path in ("/api/suggestions/delete", "/suggestions/delete"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, delete_suggestion_api(user, data))
                return
            if path in ("/api/suggestions/filter", "/suggestions/filter"):
                user = require_user(self)
                self._json(HTTPStatus.OK, filter_suggestions_api(user))
                return

            # 平台自有店铺库：差异更新 shop_uniq_tb（客户端静默调用）
            if path in ("/api/shop_uniq/upload", "/shop_uniq/upload"):
                require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, upload_shop_uniq_api(data))
                return

            # 充值下单
            if path in ("/api/pay/recharge", "/pay/recharge"):
                user = require_user(self)
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, pay_recharge_api(user, data))
                return

            # 微信支付回调（无需登录）
            if path in ("/api/pay/wechat/notify", "/pay/wechat/notify"):
                body = self._read_body()
                ok, msg = pay_wechat_notify(body, self.headers)
                if ok:
                    self._json(HTTPStatus.OK, {"code": "SUCCESS", "message": msg})
                else:
                    self._json(HTTPStatus.BAD_REQUEST, {"code": "FAIL", "message": msg})
                return

            # 支付宝回调（无需登录，form 表单）
            if path in ("/api/pay/alipay/notify", "/pay/alipay/notify"):
                from urllib.parse import parse_qs
                body = self._read_body().decode("utf-8")
                form = {k: (v[0] if v else "") for k, v in parse_qs(body).items()}
                ok, msg = pay_alipay_notify(form)
                self._text(HTTPStatus.OK, "success" if ok else "failure",
                           "text/plain; charset=utf-8")
                return

            self._json(HTTPStatus.NOT_FOUND, {"error": f"接口不存在: {path}"})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON 无效"})
        except PermissionError as e:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(e)})
        except DbError as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except SmsError as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except ValueError as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

    def do_PUT(self) -> None:
        self.do_POST()

    def _parse_upload_text(self, body: bytes) -> str:
        boundary = self._get_boundary()
        parts = body.split(b"--" + boundary)
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            head, _, content = part.partition(b"\r\n\r\n")
            if content.endswith(b"\r\n--"):
                content = content[:-4]
            elif content.endswith(b"\r\n"):
                content = content[:-2]
            if not content:
                continue
            return content.decode("utf-8", errors="replace")
        raise ValueError("未找到上传文本")

    def _parse_upload_file(self, body: bytes) -> bytes:
        boundary = self._get_boundary()
        parts = body.split(b"--" + boundary)
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            head, _, content = part.partition(b"\r\n\r\n")
            if content.endswith(b"\r\n--"):
                content = content[:-4]
            elif content.endswith(b"\r\n"):
                content = content[:-2]
            if not content:
                continue
            return content
        raise ValueError("未找到上传文件")

    def _get_boundary(self) -> bytes:
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r"boundary=(.+)", ctype)
        if not m:
            raise ValueError("multipart 缺少 boundary")
        return m.group(1).strip().strip('"').encode("ascii", errors="ignore")


def bootstrap_db() -> None:
    """启动时建表；仅当 [auth] bootstrap_username+bootstrap_password 都非空才确保测试用户。"""
    init_schema()
    cfg = _read_config()
    username = (cfg.get("auth", "bootstrap_username", fallback="") or "").strip()
    password = (cfg.get("auth", "bootstrap_password", fallback="") or "").strip()
    if not username or not password:
        print(
            "[absolute] db schema ready "
            "(skip bootstrap user: set both [auth] bootstrap_username/password in config.ini.local if needed)",
            flush=True,
        )
        return
    info = ensure_test_user(username, password)
    print(
        f"[absolute] db ready user={info['username']} id={info['id']} created={info['created']}",
        flush=True,
    )
    try:
        mig = migrate_shops_json_to_db(int(info["id"]))
        print(f"[absolute] shops migrate: {mig}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[absolute] shops migrate skipped: {e}", flush=True)


def main() -> None:
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "images").mkdir(parents=True, exist_ok=True)
    SOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_db()
    server = UnixHTTPServer(str(SOCK_PATH), Handler)
    print(f"[absolute] listening on unix:{SOCK_PATH}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if SOCK_PATH.exists():
            SOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()