#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""淘宝/天猫商品详情抓取模块。

用法:
    from fetch_item import fetch_item, fetch_detail_images

依赖 cookie(未登录淘宝会跳登录)。cookie 放在 data/cookie.txt,一行:
    cookie = "你的淘宝Cookie字符串"

返回结构:
    {
        "id": "1055688226484",
        "title": "...",
        "detail_texts": ["图文详情里的文本行..."],   # 从详情HTML提取的纯文本
        "detail_image_urls": ["https://...", ...],   # 详情图URL
        "main_image_urls": ["https://...", ...],     # 主图URL
    }
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# 淘宝详情页 HTML 接口(h5api)
DETAIL_DESC_API = "https://h5api.m.taobao.com/h5/mtop.taobao.detail.getdesc/6.0/"


def cookie_file() -> Path:
    """每次读取环境变量，避免 import 时写死路径导致客户端 Cookie 失效。"""
    env = (os.environ.get("ABSOLUTE_COOKIE_FILE") or "").strip()
    return Path(env) if env else (HERE / "data" / "cookie.txt")


# 兼容旧代码引用名；真正读写一律走 cookie_file()
COOKIE_FILE = HERE / "data" / "cookie.txt"


def load_cookie() -> str:
    """从 Cookie 文件读取。支持 'cookie=xxx' 或直接 xxx。"""
    path = cookie_file()
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    m = re.match(r"^cookie\s*=\s*(.+)$", text, re.I | re.S)
    if m:
        text = m.group(1).strip().strip('"').strip("'")
    text = text.strip()
    # 误把商品链接存成 cookie 时直接视为无效
    if text.startswith("http://") or text.startswith("https://"):
        return ""
    if text.count("=") < 3 or ";" not in text:
        return ""
    return text


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": "https://item.taobao.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    ck = load_cookie()
    if ck:
        s.headers["Cookie"] = ck
        for part in ck.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            s.cookies.set(k.strip(), v.strip(), domain=".taobao.com")
    return s


def _sync_cookie_header(s: requests.Session) -> None:
    """把 jar 里的 cookie 合并回 Cookie 请求头。"""
    jar = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())
    if not jar:
        return
    old = s.headers.get("Cookie") or ""
    merged: dict[str, str] = {}
    for part in (old + ";" + jar).split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            merged[k.strip()] = v.strip()
    s.headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in merged.items())


def _drop_cookie_keys(s: requests.Session, keys: list[str]) -> None:
    """从 session header/jar 中删除指定 cookie（用于强制刷新过期 token）。"""
    drop = {k.lower() for k in keys}
    old = s.headers.get("Cookie") or ""
    kept: list[str] = []
    for part in old.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k = part.split("=", 1)[0].strip()
        if k.lower() not in drop:
            kept.append(part)
    s.headers["Cookie"] = "; ".join(kept)
    for k in list(s.cookies.keys()):
        if k.lower() in drop:
            try:
                s.cookies.clear(domain=".taobao.com", path="/", name=k)
            except Exception:  # noqa: BLE001
                pass


def _persist_cookie(s: requests.Session) -> None:
    """把刷新后的 Cookie 写回当前 Cookie 文件。"""
    ck = (s.headers.get("Cookie") or "").strip()
    if not ck or ck.count("=") < 3:
        return
    try:
        path = cookie_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ck, encoding="utf-8")
    except OSError:
        pass


def _token_expired(ret_text: str) -> bool:
    u = (ret_text or "").upper()
    return "TOKEN_EXPIRED" in u or "TOKEN_EXOIRED" in u


def _is_wind_control(text: str) -> bool:
    """淘宝挤爆/滑块/惩罚页。"""
    t = text or ""
    u = t.upper()
    return (
        "_____TMD_____" in u
        or "X5REFERER" in u
        or "RGV587" in u
        or "FAIL_SYS_USER_VALIDATE" in u
        or "被挤爆" in t
        or "SUFEI-PUNISH" in u
        or "/PUNISH/" in u
    )


def fetch_item(item_id: str, timeout: int = 30,
               session: requests.Session | None = None) -> dict:
    """抓取单个商品:标题 + 主图URL + 详情(文本+图)。

    优先走 mtop getdetail；风控时跳过 getdesc（避免连打空包），并标记 wind_control。
    """
    s = session or _session()
    url = f"https://item.taobao.com/item.htm?id={item_id}"
    out: dict = {
        "id": item_id,
        "url": url,
        "title": "",
        "main_image_urls": [],
        "detail_texts": [],
        "detail_image_urls": [],
        "seller_id": "",
        "error": "",
        "wind_control": False,
    }
    if not load_cookie() and not (s.headers.get("Cookie") or ""):
        out["error"] = "未配置 cookie(仅扫描标题+本地图)"
        return out

    wind = False

    # 1) mtop getdetail 拿标题+主图
    try:
        mtop = fetch_item_mtop(item_id, s, timeout)
        if mtop.get("title"):
            out["title"] = mtop["title"]
            out["main_image_urls"] = mtop["main_image_urls"]
            out["seller_id"] = mtop.get("seller_id", "")
        else:
            err = mtop.get("error", "getdetail 未返回标题")
            out["error"] = err
            if _is_wind_control(err):
                wind = True
    except Exception as e:  # noqa: BLE001
        out["error"] = f"mtop 详情失败: {e}"
        if _is_wind_control(str(e)):
            wind = True

    # 2) 图文详情 getdesc —— 已风控则不再打，免得 207 次空包
    if not wind:
        try:
            detail_imgs, detail_texts = fetch_detail_images(item_id, s)
            out["detail_image_urls"] = detail_imgs
            out["detail_texts"] = detail_texts
        except Exception as e:  # noqa: BLE001
            out["error"] = (out["error"] + f" | 详情抓取失败: {e}").strip(" |")
            if _is_wind_control(str(e)):
                wind = True
    else:
        out["error"] = (out["error"] + " | 已风控，跳过 getdesc").strip(" |")

    out["wind_control"] = wind
    return out


def _mtop_token(cookie: str) -> str:
    """从 cookie 中提取 _m_h5_tk 的 token(前半段)。"""
    m = re.search(r"_m_h5_tk=([0-9a-f]+)_", cookie, re.I)
    return m.group(1) if m else ""


def _mtop_sign(token: str, t: str, appkey: str, data: str) -> str:
    """mtop 接口签名: md5(token&t&appKey&data)。"""
    return hashlib.md5(f"{token}&{t}&{appkey}&{data}".encode()).hexdigest()


def _ensure_h5_token(s: requests.Session, timeout: int = 15, force: bool = False) -> None:
    """确保有可用 _m_h5_tk；force=True 时丢弃旧 token 重新领取。"""
    cookie = s.headers.get("Cookie") or ""
    jar = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())
    if not force and (_mtop_token(cookie) or _mtop_token(jar)):
        return
    if force:
        _drop_cookie_keys(s, ["_m_h5_tk", "_m_h5_tk_enc"])
    try:
        s.get(
            "https://h5api.m.taobao.com/h5/mtop.taobao.detail.getdetail/6.0/",
            params={
                "jsv": "2.7.2", "appKey": "12574478", "t": str(int(time.time() * 1000)),
                "sign": "0", "api": "mtop.taobao.detail.getdetail", "v": "6.0",
                "type": "jsonp", "dataType": "jsonp", "callback": "mtopjsonp1",
                "data": "{}",
            },
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        pass
    _sync_cookie_header(s)
    if force:
        _persist_cookie(s)


def _mtop_log(msg: str) -> None:
    """mtop 日志：打印到控制台，并追加到 ABSOLUTE_CLIENT_LOG（若有）。"""
    print(msg, flush=True)
    log_path = (os.environ.get("ABSOLUTE_CLIENT_LOG") or "").strip()
    if not log_path:
        return
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _mtop_call(s: requests.Session, api: str, api_ver: str, data: dict, timeout: int = 30) -> dict:
    """通用 mtop 接口调用(带签名)。令牌过期可重试；滑块 SM 校验不重试（重试无意义）。"""
    _ensure_h5_token(s, timeout=min(timeout, 15))
    data_str = json.dumps(data, ensure_ascii=False)
    appkey = "12574478"

    def _once() -> dict:
        _sync_cookie_header(s)
        cookie = s.headers.get("Cookie") or ""
        t = str(int(time.time() * 1000))
        params = {
            "jsv": "2.7.2",
            "appKey": appkey,
            "t": t,
            "sign": _mtop_sign(_mtop_token(cookie), t, appkey, data_str),
            "api": api,
            "v": api_ver,
            "type": "jsonp",
            "dataType": "jsonp",
            "callback": "mtopjsonp1",
            "data": data_str,
        }
        # 带上商品域 Referer，略降风控概率
        headers = {
            "Referer": s.headers.get("Referer") or "https://detail.tmall.com/",
            "Origin": "https://detail.tmall.com",
        }
        resp = s.get(
            f"https://h5api.m.taobao.com/h5/{api}/{api_ver}/",
            params=params, timeout=timeout, headers=headers,
        )
        _sync_cookie_header(s)
        text = resp.text
        if _is_wind_control(text) and "mtopjsonp" not in text:
            raise RuntimeError(
                f"{api} 被淘宝风控拦截（惩罚页）。请稍后重试或更新 Cookie"
            )
        m = re.search(r"mtopjsonp\d*\((.*)\)\s*$", text, re.S)
        if not m:
            if _is_wind_control(text):
                raise RuntimeError(f"{api} 被淘宝风控拦截。请稍后重试或更新 Cookie")
            raise RuntimeError(f"{api} 返回异常: {text[:200]}")
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{api} JSON 解析失败: {text[:200]}") from e

    last_err: Exception | None = None
    for attempt in range(4):
        try:
            payload = _once()
        except RuntimeError as e:
            last_err = e
            err = str(e)
            # 滑块 SM / USER_VALIDATE：重试只会空耗时间
            if "SM:" in err.upper() or "USER_VALIDATE" in err.upper():
                _mtop_log(f"[mtop] {api} 需要滑块验证，停止重试: {err[:120]}")
                raise RuntimeError(
                    f"{api} 触发淘宝滑块验证(RGV587/SM)。"
                    "请用 Chrome 打开淘宝完成验证后重新「读 Cookie」，或先只抓当前商品链接。"
                ) from e
            if _is_wind_control(err) and attempt < 3:
                wait = (3, 8, 20)[attempt]
                _mtop_log(f"[mtop] {api} 风控，{wait}s 后重试 ({attempt+1}/3)")
                time.sleep(wait)
                _ensure_h5_token(s, timeout=min(timeout, 15), force=True)
                continue
            raise
        ret_u = " ".join(str(x) for x in (payload.get("ret") or []))
        if _token_expired(ret_u):
            _ensure_h5_token(s, timeout=min(timeout, 15), force=True)
            payload = _once()
            ret_u = " ".join(str(x) for x in (payload.get("ret") or []))
            if _token_expired(ret_u):
                raise RuntimeError(f"{api} 令牌过期且刷新失败，请重新粘贴浏览器 Cookie")
            _persist_cookie(s)
            return payload
        if "SM:" in ret_u.upper() or "USER_VALIDATE" in ret_u.upper():
            _mtop_log(f"[mtop] {api} {ret_u[:80]}（滑块，不重试）")
            raise RuntimeError(
                f"{api} 触发淘宝滑块验证: {ret_u}。"
                "请用 Chrome 打开淘宝完成验证后重新「读 Cookie」，或先只抓当前商品链接。"
            )
        if _is_wind_control(ret_u) and attempt < 3:
            wait = (3, 8, 20)[attempt]
            _mtop_log(f"[mtop] {api} {ret_u[:40]}，{wait}s 后重试 ({attempt+1}/3)")
            time.sleep(wait)
            _ensure_h5_token(s, timeout=min(timeout, 15), force=True)
            continue
        if _is_wind_control(ret_u):
            raise RuntimeError(f"{api} 被淘宝风控拦截: {ret_u}")
        return payload
    if last_err:
        raise last_err
    raise RuntimeError(f"{api} 重试耗尽")

def fetch_item_mtop(item_id: str, s: requests.Session | None = None, timeout: int = 30) -> dict:
    """用 mtop getdetail 接口抓标题+主图+sellerId。

    返回 {"title", "main_image_urls", "seller_id", "error"}, 失败时 title 为空。
    """
    s = s or _session()
    try:
        data = _mtop_call(s, "mtop.taobao.detail.getdetail", "6.0",
                          {"itemNumId": item_id, "exParams": "{}"}, timeout)
    except Exception as e:  # noqa: BLE001
        return {"title": "", "main_image_urls": [], "seller_id": "", "error": str(e)}
    ret = data.get("ret") or []
    if ret and any("FAIL" in str(r) for r in ret):
        return {"title": "", "main_image_urls": [], "seller_id": "", "error": str(ret[0])}
    item = (data.get("data") or {}).get("item") or {}
    title = str(item.get("title") or "").strip()
    imgs = item.get("images") or []
    if isinstance(imgs, str):
        imgs = [u for u in imgs.split(",") if u]
    imgs = [u if u.startswith("http") else "https:" + u for u in imgs]
    imgs = [re.sub(r"_[0-9]+x[0-9]+.*$|_\.webp$", "", u) for u in imgs]
    seller_id = str(item.get("sellerId") or (data.get("data") or {}).get("sellerId") or "")
    return {
        "title": title,
        "main_image_urls": imgs[:8],
        "seller_id": seller_id,
        "error": "" if title else "getdetail 未返回标题字段",
    }


def fetch_detail_images(item_id: str, s: requests.Session | None = None, timeout: int = 30):
    """调淘宝 mtop 接口拿图文详情, 返回 (图片URL列表, 文本行列表)。"""
    s = s or _session()
    try:
        data = _mtop_call(s, "mtop.taobao.detail.getdesc", "6.0", {"id": item_id}, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"详情接口请求失败: {e}") from e

    ret = " ".join(str(x) for x in (data.get("ret") or []))
    if "login.taobao.com" in str(data) or "RGV587" in ret or "FAIL_SYS_USER_VALIDATE" in ret:
        raise RuntimeError("详情接口要求登录/验证，Cookie 无效或已过期")

    desc_html = ""
    try:
        desc_html = data["data"]["desc"]
    except (KeyError, TypeError):
        # 新版接口返回 pcDescContent(含 HTML 或 JSON 字符串)
        try:
            desc_html = data["data"]["pcDescContent"]
        except (KeyError, TypeError):
            raise RuntimeError(f"详情接口无 desc 字段: {str(data)[:200]}")
    # pcDescContent 可能是 JSON 字符串包着 desc
    if desc_html.startswith("{"):
        try:
            inner = json.loads(desc_html)
            if isinstance(inner, dict):
                desc_html = inner.get("desc") or inner.get("pcDescContent") or desc_html
        except json.JSONDecodeError:
            pass

    soup = BeautifulSoup(desc_html, "html.parser")
    img_urls = [img["src"] for img in soup.find_all("img") if img.get("src")]
    # 相对路径补全
    img_urls = [u if u.startswith("http") else "https:" + u for u in img_urls]
    # 去除参数尾巴
    img_urls = [re.sub(r"_[0-9]+x[0-9]+.*$|_\.webp$", "", u) for u in img_urls]

    texts = [t.strip() for t in soup.get_text("\n").splitlines() if t.strip()]

    # 兜底:图片 URL 从 css 背景里挖(部分详情图以 background-image 内嵌)
    for m2 in re.finditer(r"background-image:\s*url\((['\"]?)(//img\.alicdn\.com[^)'\"]+)\1\)", desc_html):
        u = "https:" + m2.group(2)
        if u not in img_urls:
            img_urls.append(u)

    return img_urls, texts


if __name__ == "__main__":
    import sys

    iid = sys.argv[1] if len(sys.argv) > 1 else "1055688226484"
    r = fetch_item(iid)
    print(json.dumps(r, ensure_ascii=False, indent=2)[:2000])
