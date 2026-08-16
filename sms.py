#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿里云短信。缺配置或发送失败直接报错，不假装发出。AK/SK 只读 config.ini.local。"""

from __future__ import annotations

import base64
import configparser
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.ini"
CONFIG_LOCAL_PATH = ROOT / "config.ini.local"


class SmsError(RuntimeError):
    pass


def _read_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    if not CONFIG_PATH.is_file():
        raise SmsError(f"缺少配置文件: {CONFIG_PATH}")
    cfg.read(str(CONFIG_PATH), encoding="utf-8")
    if CONFIG_LOCAL_PATH.is_file():
        cfg.read(str(CONFIG_LOCAL_PATH), encoding="utf-8")
    return cfg


def _truthy(val: str) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


def sms_settings() -> dict[str, str]:
    cfg = _read_config()
    sec = "aliyun_sms"
    get = lambda key, default="": (
        cfg.get(sec, key, fallback=default) if cfg.has_section(sec) else default
    ).strip()
    return {
        "enabled": get("enabled", "0"),
        "sign_name": get("sign_name"),
        "template_code": get("template_code"),
        "access_key_id": get("access_key_id"),
        "access_key_secret": get("access_key_secret"),
    }


def _sign(params: dict, secret: str) -> str:
    canonical = urllib.parse.urlencode(sorted(params.items()), quote_via=urllib.parse.quote)
    string_to_sign = "POST&%2F&" + urllib.parse.quote(canonical, safe="")
    key = (secret + "&").encode("utf-8")
    digest = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_sms_code(phone: str, code: str) -> None:
    phone = str(phone or "").strip()
    code = str(code or "").strip()
    if not phone:
        raise SmsError("手机号不能为空")
    if not code:
        raise SmsError("验证码不能为空")
    s = sms_settings()
    if not _truthy(s["enabled"]):
        raise SmsError("短信服务未启用（config [aliyun_sms] enabled）")
    missing = [
        k
        for k in ("access_key_id", "access_key_secret", "sign_name", "template_code")
        if not s[k]
    ]
    if missing:
        raise SmsError(
            "短信服务缺少配置: "
            + ", ".join(missing)
            + "（AK/SK 写 config.ini.local [aliyun_sms]）"
        )
    params = {
        "Action": "SendSms",
        "Version": "2017-05-25",
        "AccessKeyId": s["access_key_id"],
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": str(uuid.uuid4()),
        "SignatureVersion": "1.0",
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "PhoneNumbers": phone,
        "SignName": s["sign_name"],
        "TemplateCode": s["template_code"],
        "TemplateParam": json.dumps({"code": code}, ensure_ascii=False, separators=(",", ":")),
    }
    params["Signature"] = _sign(params, s["access_key_secret"])
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        "https://dysmsapi.aliyuncs.com/",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise SmsError(f"短信发送失败: HTTP {e.code} {raw[:200]}") from e
    except Exception as e:
        raise SmsError(f"短信发送失败: {e}") from e
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SmsError(f"短信接口返回非 JSON: {raw[:200]}") from e
    if payload.get("Code") != "OK":
        raise SmsError(payload.get("Message") or payload.get("Code") or "短信发送失败")
