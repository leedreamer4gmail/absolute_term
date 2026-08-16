#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""支付 / 短信配置：全部从 config.ini(+local) 与环境变量读取，禁止硬编码密钥。"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.ini"
CONFIG_LOCAL_PATH = ROOT / "config.ini.local"


def _read_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if CONFIG_PATH.is_file():
        cfg.read(str(CONFIG_PATH), encoding="utf-8")
    if CONFIG_LOCAL_PATH.is_file():
        cfg.read(str(CONFIG_LOCAL_PATH), encoding="utf-8")
    return cfg


def _get(section: str, key: str, default: str = "") -> str:
    cfg = _read_config()
    env_key = f"AT_{section}_{key}".upper()
    env_val = os.getenv(env_key)
    if env_val is not None and str(env_val).strip() != "":
        return str(env_val).strip()
    if cfg.has_option(section, key):
        return cfg.get(section, key, fallback=default).strip()
    return default


def _get_any(section: str, keys: tuple[str, ...], default: str = "") -> str:
    for k in keys:
        v = _get(section, k, "")
        if v:
            return v
    return default


def public_base() -> str:
    """完整对外 base，如 https://leedreamer.cn/absolute_term"""
    base = _get("app", "public_base", "") or _get("ui", "site_base", "")
    if not base:
        # 兼容旧配置：仅有路径前缀时拼默认域名（域名也可被环境变量覆盖）
        prefix = _get("ui", "public_base", "/absolute_term").rstrip("/") or "/absolute_term"
        host = os.getenv("AT_PUBLIC_HOST", "https://leedreamer.cn").rstrip("/")
        base = host + prefix
    return base.rstrip("/")


def wxpay_cfg() -> dict:
    notify = _get("wxpay", "notify_url", "") or f"{public_base()}/api/pay/wechat/notify"
    mch = _get_any("wxpay", ("mch_id", "mchid"), "")
    app = _get_any("wxpay", ("app_id", "appid"), "")
    pkey = _get("wxpay", "private_key_path", "")
    serial = _get_any("wxpay", ("cert_serial_no", "serial_no"), "")
    return {
        "app_id": app,
        "mch_id": mch,
        "api_v3_key": os.getenv("AT_WXPAY_API_V3_KEY", "") or os.getenv("LS_WXPAY_API_V3_KEY", "") or _get("wxpay", "api_v3_key", ""),
        "private_key_path": pkey,
        "cert_serial_no": serial,
        "platform_cert_path": _get("wxpay", "platform_cert_path", ""),
        "notify_url": notify,
        "enabled": bool(mch and app and pkey and serial),
    }


def zfbpay_cfg() -> dict:
    notify = _get("zfbpay", "notify_url", "") or f"{public_base()}/api/pay/alipay/notify"
    return_url = _get("zfbpay", "return_url", "") or f"{public_base()}/"
    priv = _get("zfbpay", "private_key", "") or os.getenv("AT_ZFB_PRIVATE_KEY", "") or os.getenv("LS_ZFB_PRIVATE_KEY", "")
    priv_path = _get("zfbpay", "private_key_path", "")
    has_key = bool(priv or (priv_path and os.path.exists(priv_path)))
    app = _get_any("zfbpay", ("app_id", "appid"), "")
    return {
        "app_id": app,
        "private_key": priv,
        "private_key_path": priv_path,
        "alipay_public_key": _get("zfbpay", "alipay_public_key", "") or os.getenv("AT_ZFB_ALIPAY_PUBLIC_KEY", "") or os.getenv("LS_ZFB_ALIPAY_PUBLIC_KEY", ""),
        "alipay_public_key_path": _get("zfbpay", "alipay_public_key_path", ""),
        "notify_url": notify,
        "return_url": return_url,
        "gateway": _get("zfbpay", "gateway", "https://openapi.alipay.com/gateway.do") or "https://openapi.alipay.com/gateway.do",
        "seller_id": _get("zfbpay", "seller_id", ""),
        "enabled": bool(app and has_key),
    }


def sms_cfg() -> dict:
    enabled_raw = _get("aliyun_sms", "enabled", "true").lower()
    return {
        "enabled": enabled_raw in ("1", "true", "yes", "on"),
        "sign_name": _get("aliyun_sms", "sign_name", ""),
        "template_code": _get("aliyun_sms", "template_code", ""),
        "access_key_id": (
            os.getenv("ALIYUN_SMS_ACCESS_KEY_ID")
            or os.getenv("AT_ALIYUN_SMS_ACCESS_KEY_ID")
            or _get("aliyun_sms", "access_key_id", "")
        ),
        "access_key_secret": (
            os.getenv("ALIYUN_SMS_ACCESS_KEY_SECRET")
            or os.getenv("AT_ALIYUN_SMS_ACCESS_KEY_SECRET")
            or _get("aliyun_sms", "access_key_secret", "")
        ),
    }
