"""微信 / 支付宝支付网关（同步 requests 版，配置来自 pay_config）。"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
from pathlib import Path

import requests

from pay_config import public_base, wxpay_cfg, zfbpay_cfg


def _wxpay_sign_auth(method: str, url_path: str, body: str, cfg: dict) -> str | None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    pkey_path = cfg.get("private_key_path", "")
    if not pkey_path or not os.path.exists(pkey_path):
        return None
    try:
        with open(pkey_path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
    except Exception:
        return None
    timestamp = str(int(time.time()))
    nonce_str = secrets.token_hex(16)
    sign_str = f"{method.upper()}\n{url_path}\n{timestamp}\n{nonce_str}\n{body}\n"
    signature = base64.b64encode(
        private_key.sign(sign_str.encode("utf-8"), asym_padding.PKCS1v15(), hashes.SHA256())
    ).decode("utf-8")
    return (
        f'WECHATPAY2-SHA256-RSA2048 mchid="{cfg["mch_id"]}",'
        f'serial_no="{cfg["cert_serial_no"]}",'
        f'nonce_str="{nonce_str}",'
        f'timestamp="{timestamp}",'
        f'signature="{signature}"'
    )


def wxpay_aes_decrypt(api_v3_key: str, associated_data: str, nonce: str, ciphertext: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = api_v3_key.encode("utf-8")
    data = base64.b64decode(ciphertext)
    plaintext = AESGCM(key).decrypt(nonce.encode("utf-8"), data, associated_data.encode("utf-8"))
    return plaintext.decode("utf-8")


def wxpay_verify_notify(headers, raw_body: bytes, *, max_clock_skew_seconds: int = 300) -> bool:
    """使用微信支付平台证书验证回调签名、证书序列号和时间窗口。"""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    cfg = wxpay_cfg()
    cert_path = str(cfg.get("platform_cert_path") or "").strip()
    timestamp = str(headers.get("Wechatpay-Timestamp") or headers.get("wechatpay-timestamp") or "").strip()
    nonce = str(headers.get("Wechatpay-Nonce") or headers.get("wechatpay-nonce") or "").strip()
    signature = str(headers.get("Wechatpay-Signature") or headers.get("wechatpay-signature") or "").strip()
    serial = str(headers.get("Wechatpay-Serial") or headers.get("wechatpay-serial") or "").strip()
    if not cert_path or not Path(cert_path).is_file() or not all((timestamp, nonce, signature, serial)):
        return False
    try:
        ts = int(timestamp)
        if abs(int(time.time()) - ts) > max_clock_skew_seconds:
            return False
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
        if format(cert.serial_number, "X").lstrip("0").upper() != serial.lstrip("0").upper():
            return False
        message = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + raw_body + b"\n"
        cert.public_key().verify(base64.b64decode(signature), message, asym_padding.PKCS1v15(), hashes.SHA256())
        return True
    except (ValueError, InvalidSignature, OSError):
        return False


def wxpay_create_native(amount_cents: int, order_no: str, description: str) -> dict:
    cfg = wxpay_cfg()
    if not cfg["enabled"]:
        return {"error": "微信支付未配置"}
    url_path = "/v3/pay/transactions/native"
    body = json.dumps(
        {
            "appid": cfg["app_id"],
            "mchid": cfg["mch_id"],
            "description": description,
            "out_trade_no": order_no,
            "notify_url": cfg["notify_url"],
            "amount": {"total": amount_cents, "currency": "CNY"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    auth = _wxpay_sign_auth("POST", url_path, body, cfg)
    if not auth:
        return {"error": "微信支付未配置商户私钥"}
    headers = {
        "Authorization": auth,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "absolute-term/1.0",
    }
    try:
        resp = requests.post(
            f"https://api.mch.weixin.qq.com{url_path}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=30.0,
        )
        data = resp.json()
    except Exception as exc:
        return {"error": f"请求微信支付失败：{exc}"}
    if "code_url" in data:
        return {"code_url": data["code_url"]}
    return {"error": data.get("message") or data.get("code") or "微信下单失败"}


def wxpay_query(order_no: str) -> dict:
    cfg = wxpay_cfg()
    mch_id = cfg.get("mch_id", "")
    url_path = f"/v3/pay/transactions/out-trade-no/{order_no}?mchid={mch_id}"
    auth = _wxpay_sign_auth("GET", url_path, "", cfg)
    if not auth:
        return {"error": "未配置商户私钥"}
    try:
        resp = requests.get(
            f"https://api.mch.weixin.qq.com{url_path}",
            headers={
                "Authorization": auth,
                "Accept": "application/json",
                "User-Agent": "absolute-term/1.0",
            },
            timeout=15.0,
        )
        data = resp.json()
    except Exception as exc:
        return {"error": f"查询失败：{exc}"}
    return {
        "trade_state": data.get("trade_state", ""),
        "amount": int((data.get("amount") or {}).get("total", 0)),
        "raw": data,
    }


def _pem_wrap(raw: str, header: str) -> str:
    raw = raw.strip()
    if "-----BEGIN" in raw:
        return raw
    body = "\n".join(raw[i : i + 64] for i in range(0, len(raw), 64))
    return f"-----BEGIN {header}-----\n{body}\n-----END {header}-----\n"


def _zfb_load_private_key(cfg: dict):
    from cryptography.hazmat.primitives import serialization

    raw = (cfg.get("private_key") or "").strip()
    if not raw:
        path = (cfg.get("private_key_path") or "").strip()
        if path and os.path.exists(path):
            raw = Path(path).read_text(encoding="utf-8")
    if not raw:
        path = Path(__file__).resolve().parent / "secrets" / "alipay_private.pem"
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
    if not raw:
        return None
    try:
        pem = _pem_wrap(raw, "PRIVATE KEY")
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception:
        return None


def _zfb_load_public_key(cfg: dict):
    from cryptography.hazmat.primitives import serialization

    raw = (cfg.get("alipay_public_key") or "").strip()
    if not raw:
        path = (cfg.get("alipay_public_key_path") or "").strip()
        if path and os.path.exists(path):
            raw = Path(path).read_text(encoding="utf-8")
    if not raw:
        path = Path(__file__).resolve().parent / "secrets" / "alipay_public.pem"
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
    if not raw:
        return None
    try:
        pem = _pem_wrap(raw, "PUBLIC KEY")
        return serialization.load_pem_public_key(pem.encode("utf-8"))
    except Exception:
        return None


def _zfb_sign_content(params: dict) -> str:
    items = [(k, v) for k, v in params.items() if k != "sign" and v not in (None, "")]
    items.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)


def _zfb_sign(params: dict, cfg: dict) -> str | None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    private_key = _zfb_load_private_key(cfg)
    if private_key is None:
        return None
    content = _zfb_sign_content(params)
    signature = private_key.sign(content.encode("utf-8"), asym_padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")


def zfb_verify(params: dict) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    cfg = zfbpay_cfg()
    public_key = _zfb_load_public_key(cfg)
    if public_key is None:
        return False
    sign = params.get("sign", "")
    if not sign:
        return False
    items = [(k, v) for k, v in params.items() if k not in ("sign", "sign_type") and v not in (None, "")]
    items.sort(key=lambda kv: kv[0])
    content = "&".join(f"{k}={v}" for k, v in items)
    try:
        public_key.verify(
            base64.b64decode(sign),
            content.encode("utf-8"),
            asym_padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except (InvalidSignature, Exception):
        return False


def zfb_build_page_pay_url(order_no: str, amount_cents: int, subject: str) -> dict:
    """电脑网站支付（alipay.trade.page.pay）。与 keng 一致；当面付 precreate 本应用未开通会 ACQ.ACCESS_FORBIDDEN。"""
    from urllib.parse import quote_plus

    cfg = zfbpay_cfg()
    if not cfg["enabled"]:
        return {"error": "支付宝未配置"}
    biz_content = json.dumps(
        {
            "out_trade_no": order_no,
            "total_amount": f"{amount_cents / 100:.2f}",
            "subject": subject,
            "product_code": "FAST_INSTANT_TRADE_PAY",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    params = {
        "app_id": cfg["app_id"],
        "method": "alipay.trade.page.pay",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "version": "1.0",
        "notify_url": cfg["notify_url"],
        "return_url": cfg.get("return_url") or f"{public_base()}/",
        "biz_content": biz_content,
    }
    sign = _zfb_sign(params, cfg)
    if sign is None:
        return {"error": "未配置应用私钥"}
    params["sign"] = sign
    query = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    return {"pay_url": f"{cfg['gateway']}?{query}"}


def zfb_query(order_no: str) -> dict:
    from urllib.parse import quote_plus

    cfg = zfbpay_cfg()
    biz_content = json.dumps({"out_trade_no": order_no}, ensure_ascii=False, separators=(",", ":"))
    params = {
        "app_id": cfg["app_id"],
        "method": "alipay.trade.query",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "version": "1.0",
        "biz_content": biz_content,
    }
    sign = _zfb_sign(params, cfg)
    if sign is None:
        return {"error": "未配置应用私钥"}
    params["sign"] = sign
    body = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    try:
        resp = requests.post(
            cfg["gateway"],
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            timeout=15.0,
        )
        data = resp.json()
    except Exception as exc:
        return {"error": f"查询失败：{exc}"}
    node = data.get("alipay_trade_query_response", {})
    if str(node.get("code")) != "10000":
        return {"error": node.get("sub_msg") or node.get("msg") or "查询失败"}
    try:
        amount_cents = int(round(float(node.get("total_amount", "0")) * 100))
    except Exception:
        amount_cents = 0
    return {"trade_status": node.get("trade_status", ""), "amount_cents": amount_cents}
