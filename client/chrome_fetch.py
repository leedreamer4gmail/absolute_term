#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机 Chrome（CDP）抓淘宝详情，绕过 requests/mtop 的 RGV587 滑块。

流程：
1. 「开始抓取」自动 ensure_chrome / 读 Cookie（CDP 或专用配置）
2. 若未登录：专用 Chrome 等待用户登录后继续
3. Playwright connect_over_cdp 截获 getdetail/getdesc；滑块可自动拖
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from shop_store import chrome_profile_dir, cookie_file_path, writable_data_dir

HERE = Path(__file__).resolve().parent
DATA = writable_data_dir()
PROFILE_DIR = chrome_profile_dir()
COOKIE_PATH = cookie_file_path()
DEFAULT_DEBUG_PORT = 9333  # 避开常见 9222，减少冲突


def _log(msg: str) -> None:
    print(msg, flush=True)
    log_path = (os.environ.get("ABSOLUTE_CLIENT_LOG") or "").strip()
    if not log_path:
        return
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _login_page_error(url: str, title: str, body: str) -> str:
    u = (url or "").lower()
    if any(x in u for x in ("login.taobao.com", "login.tmall.com", "login.alibaba.com")):
        return "页面跳到淘宝登录页，Cookie 无效或未登录"
    try:
        from shop_store import is_junk_product_title
    except ImportError:
        is_junk_product_title = lambda t: str(t or "").strip() in ("登录", "请登录")  # noqa: E731
    t = (title or "").strip()
    if t and is_junk_product_title(t):
        return f"页面标题是「{t}」，不是商品（多半未登录）"
    head = (body or "")[:8000]
    if ("密码登录" in head or "短信登录" in head) and "item.htm" not in u:
        return "页面像登录墙，未拿到商品"
    return ""


def find_chrome_exe() -> str:
    """定位本机 Google Chrome；找不到就报错，绝不悄悄改开 Edge。"""
    candidates: list[str] = []
    which = shutil.which("chrome") or shutil.which("google-chrome")
    if which:
        candidates.append(which)
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    for base in (local, pf, pf86):
        if not base:
            continue
        candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    for path in candidates:
        if path and Path(path).is_file():
            return path
    raise FileNotFoundError(
        "未找到 Google Chrome。请安装 Chrome；开始抓取时会自动打开专用配置登录淘宝。"
        "不要用系统默认浏览器（常是 Edge）。"
    )


def debug_port() -> int:
    raw = (os.environ.get("ABSOLUTE_CHROME_DEBUG_PORT") or "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_DEBUG_PORT


def cdp_endpoint(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or debug_port()}"


def cdp_alive(port: int | None = None) -> bool:
    try:
        with urllib.request.urlopen(cdp_endpoint(port) + "/json/version", timeout=1.5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ensure_chrome(url: str = "https://www.taobao.com/", port: int | None = None) -> int:
    """确保调试 Chrome 已启动；已在跑则复用。返回端口。"""
    port = port or debug_port()
    DATA.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    if cdp_alive(port):
        _log(f"[chrome] 复用已运行调试 Chrome :{port}")
        if url:
            # 新开标签打开目标页（HTTP 接口）
            try:
                req = urllib.request.Request(
                    cdp_endpoint(port) + "/json/new?" + urllib.parse.quote(url, safe=":/?&="),
                    method="PUT",
                )
                urllib.request.urlopen(req, timeout=3).read()
            except Exception:  # noqa: BLE001
                pass
        return port

    chrome = find_chrome_exe()
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        url or "https://www.taobao.com/",
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _log(f"[chrome] 启动调试 Chrome :{port} profile={PROFILE_DIR}")
    deadline = time.time() + 20
    while time.time() < deadline:
        if cdp_alive(port):
            return port
        time.sleep(0.3)
    raise RuntimeError(
        f"Chrome 调试端口 {port} 未就绪。请关闭占用该配置的 Chrome 后重试。"
        f"\n配置目录: {PROFILE_DIR}"
    )


def _parse_mtop_body(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"mtopjsonp\d*\((.*)\)\s*$", text, re.S)
    raw = m.group(1) if m else text
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _is_sm_payload(obj: dict | None) -> bool:
    if not obj:
        return False
    ret = " ".join(str(x) for x in (obj.get("ret") or []))
    u = ret.upper()
    return "RGV587" in u or "USER_VALIDATE" in u or "SM:" in u


def _from_getdetail(obj: dict) -> dict:
    item = (obj.get("data") or {}).get("item") or {}
    title = str(item.get("title") or "").strip()
    imgs = item.get("images") or []
    if isinstance(imgs, str):
        imgs = [u for u in imgs.split(",") if u]
    imgs = [u if str(u).startswith("http") else "https:" + str(u) for u in imgs]
    imgs = [re.sub(r"_[0-9]+x[0-9]+.*$|_\.webp$", "", u) for u in imgs]
    seller_id = str(item.get("sellerId") or (obj.get("data") or {}).get("sellerId") or "")
    return {"title": title, "main_image_urls": imgs[:8], "seller_id": seller_id}


def _from_getdesc(obj: dict) -> tuple[list[str], list[str]]:
    data = obj.get("data") or {}
    desc_html = data.get("desc") or data.get("pcDescContent") or ""
    if isinstance(desc_html, dict):
        desc_html = desc_html.get("desc") or desc_html.get("pcDescContent") or ""
    if isinstance(desc_html, str) and desc_html.startswith("{"):
        try:
            inner = json.loads(desc_html)
            if isinstance(inner, dict):
                desc_html = inner.get("desc") or inner.get("pcDescContent") or desc_html
        except json.JSONDecodeError:
            pass
    html = str(desc_html or "")
    imgs = re.findall(r'(?:https?:)?//[^"\'\s<>]+\.(?:jpg|jpeg|png|webp)', html, re.I)
    urls: list[str] = []
    for u in imgs:
        u = u if u.startswith("http") else "https:" + u
        u = re.sub(r"_[0-9]+x[0-9]+.*$|_\.webp$", "", u)
        if u not in urls:
            urls.append(u)
    texts: list[str] = []
    for line in re.findall(r">([^<]{2,80})<", html):
        t = re.sub(r"\s+", " ", line).strip()
        if t and t not in texts and not t.startswith("http"):
            texts.append(t)
    return urls[:40], texts[:80]


def _persist_cookies(cookies: list[dict]) -> str:
    parts = [
        f"{c['name']}={c['value']}"
        for c in cookies
        if c.get("name") and c.get("value") is not None
        and ("taobao" in (c.get("domain") or "") or "tmall" in (c.get("domain") or ""))
    ]
    if not parts:
        return ""
    ck = "; ".join(parts)
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        path = Path(os.environ.get("ABSOLUTE_COOKIE_FILE") or COOKIE_PATH)
        path.write_text(ck, encoding="utf-8")
    except OSError:
        pass
    return ck


class ChromeFetcher:
    """通过 CDP 连接已启动的调试 Chrome，截获详情接口。"""

    def __init__(self, port: int | None = None) -> None:
        self.port = port or debug_port()
        self._pw = None
        self._browser = None
        self._ctx = None

    def start(self) -> None:
        if self._ctx is not None:
            return
        ensure_chrome(port=self.port)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "未安装 playwright。请执行: python -m pip install playwright"
            ) from e
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(cdp_endpoint(self.port))
        except Exception as e:  # noqa: BLE001
            self.close()
            raise RuntimeError(f"无法连接 Chrome CDP {cdp_endpoint(self.port)}: {e}") from e
        if not self._browser.contexts:
            raise RuntimeError("Chrome 无可用上下文，请重新点「打开 Chrome」")
        self._ctx = self._browser.contexts[0]
        _log(f"[chrome] 已连接 CDP :{self.port}")

    def close(self) -> None:
        """只停 Playwright 驱动，绝不关掉用户正在用的调试 Chrome。"""
        self._ctx = None
        # connect_over_cdp 时 browser.close() 会把真 Chrome 一起杀掉，禁止调用
        self._browser = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = None

    def open_url(self, url: str) -> None:
        ensure_chrome(url=url or "https://www.taobao.com/", port=self.port)

    def fetch_item(
        self,
        item_id: str,
        *,
        timeout: float = 60,
        slider_wait: float = 180,
        progress_cb: Callable[[str], None] | None = None,
    ) -> dict:
        item_id = str(item_id or "").strip()
        if not item_id.isdigit():
            raise ValueError(f"商品 id 无效: {item_id!r}")
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
            "via": "chrome",
        }
        self.start()
        assert self._ctx is not None
        page = self._ctx.new_page()
        captured: dict = {"detail": None, "desc": None, "sm": False}

        def on_response(resp) -> None:
            try:
                u = resp.url or ""
                if "mtop.taobao.detail.getdetail" not in u and "mtop.taobao.detail.getdesc" not in u:
                    return
                text = resp.text()
            except Exception:  # noqa: BLE001
                return
            obj = _parse_mtop_body(text)
            if obj is None:
                return
            if _is_sm_payload(obj):
                captured["sm"] = True
                return
            if "mtop.taobao.detail.getdetail" in u:
                captured["detail"] = obj
                captured["sm"] = False
            elif "mtop.taobao.detail.getdesc" in u:
                captured["desc"] = obj

        page.on("response", on_response)
        if progress_cb:
            progress_cb(f"Chrome 打开商品页 {item_id}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        except Exception as e:  # noqa: BLE001
            out["error"] = f"Chrome 打开商品页失败: {e}"
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            return out

        try:
            login_err = _login_page_error(page.url or "", "", "")
        except Exception:  # noqa: BLE001
            login_err = ""
        if login_err:
            out["error"] = login_err
            _log(f"[chrome] {login_err} id={item_id}")
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            return out

        deadline = time.time() + max(float(slider_wait), 30.0)
        warned = False
        title_ready_at: float | None = None
        while time.time() < deadline:
            if captured["detail"] and not _is_sm_payload(captured["detail"]):
                parsed = _from_getdetail(captured["detail"])
                if parsed.get("title"):
                    out["title"] = parsed["title"]
                    out["main_image_urls"] = parsed["main_image_urls"] or out["main_image_urls"]
                    out["seller_id"] = parsed["seller_id"] or out["seller_id"]
                    out["wind_control"] = False
                    if title_ready_at is None:
                        title_ready_at = time.time()
            if captured["desc"] and not _is_sm_payload(captured["desc"]):
                imgs, texts = _from_getdesc(captured["desc"])
                out["detail_image_urls"] = imgs
                out["detail_texts"] = texts

            body = ""
            try:
                body = page.content()
            except Exception:  # noqa: BLE001
                pass
            if (
                "被挤爆" in body
                or "_____tmd_____" in body.lower()
                or "punish" in body.lower()
                or captured["sm"]
            ) and not out["title"]:
                out["wind_control"] = True
                if not warned:
                    msg = "淘宝滑块/挤爆：尝试自动拖动滑块（失败则请手动）"
                    _log(f"[chrome] {msg}")
                    if progress_cb:
                        progress_cb(msg)
                    warned = True
                    try_auto_slider(page, progress_cb=progress_cb)

            if not out.get("title"):
                try:
                    dom_title = page.evaluate(
                        """() => {
                          const h = document.querySelector('h1');
                          if (h && h.innerText) return h.innerText.trim();
                          const m = document.querySelector('meta[property="og:title"]');
                          if (m) return (m.getAttribute('content') || '').trim();
                          return (document.title || '').replace(/-淘宝网$/,'').trim();
                        }"""
                    )
                    if dom_title and "验证" not in dom_title and "挤爆" not in dom_title:
                        if not _login_page_error("", str(dom_title), ""):
                            out["title"] = str(dom_title)[:200]
                            if title_ready_at is None:
                                title_ready_at = time.time()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    dom_imgs = page.evaluate(
                        """() => Array.from(document.querySelectorAll('img'))
                          .map(i => i.src || i.getAttribute('data-src') || '')
                          .filter(u => /alicdn|taobao|tmall/i.test(u))
                          .slice(0, 12)"""
                    )
                    if isinstance(dom_imgs, list) and dom_imgs and not out["main_image_urls"]:
                        out["main_image_urls"] = [
                            (u if str(u).startswith("http") else "https:" + str(u))
                            for u in dom_imgs
                        ][:8]
                except Exception:  # noqa: BLE001
                    pass

            if out.get("title") and (out["detail_image_urls"] or out["detail_texts"]):
                break
            # 有标题后最多再等 20s 等 getdesc
            if out.get("title") and title_ready_at and time.time() - title_ready_at > 20:
                break
            time.sleep(0.5)

        try:
            cur_url = page.url or ""
        except Exception:  # noqa: BLE001
            cur_url = ""
        login_err = _login_page_error(cur_url, out.get("title") or "", "")
        if login_err:
            out["error"] = login_err
            out["title"] = ""
            _log(f"[chrome] {login_err} id={item_id}")

        try:
            page.remove_listener("response", on_response)
        except Exception:  # noqa: BLE001
            pass
        try:
            _persist_cookies(self._ctx.cookies())
        except Exception:  # noqa: BLE001
            pass
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

        if not out["title"] and out["wind_control"]:
            out["error"] = (
                "Chrome 页仍处于淘宝滑块/挤爆验证，未拿到标题。"
                "请完成验证后重新点「开始抓取」。"
            )
        elif not out["title"]:
            out["error"] = "Chrome 未解析到商品标题（请先在调试 Chrome 登录淘宝）"
        elif not (out["detail_texts"] or out["detail_image_urls"]):
            out["error"] = (out["error"] + " | Chrome 未截获图文详情 getdesc").strip(" |")
        return out

    def collect_item_ids(
        self,
        url: str,
        *,
        wait_seconds: float = 8.0,
        progress_cb: Callable[[str], None] | None = None,
    ) -> list[str]:
        """打开任意淘宝页，从 DOM + 网络响应抽取商品 id（不抓详情）。"""
        url = (url or "").strip()
        if not url:
            raise ValueError("采链 URL 为空")
        wait_seconds = float(wait_seconds)
        if wait_seconds < 1:
            raise ValueError("采链等待秒数必须 ≥ 1")

        def _p(msg: str) -> None:
            if progress_cb:
                progress_cb(msg)
            _log(msg)

        self.start()
        assert self._ctx is not None
        page = self._ctx.new_page()
        captured: list[str] = []
        id_re = re.compile(
            r"(?:itemId|item_id|itemNumId|auctionId|nid)[\"']?\s*[:=]\s*[\"']?(\d{5,})",
            re.I,
        )

        def on_response(resp) -> None:
            try:
                u = (resp.url or "").lower()
                if "mtop" not in u and "item" not in u and "search" not in u:
                    return
                text = resp.text()[:200000]
            except Exception:  # noqa: BLE001
                return
            captured.extend(id_re.findall(text))

        page.on("response", on_response)
        nav_timeout = max(int(wait_seconds * 1000) + 15000, 60000)
        try:
            _p(f"[chrome] 采链打开 {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
            except Exception as e:  # noqa: BLE001
                _p(f"[chrome] 采链导航未完成（继续抽 id）: {e}")
            try_auto_slider(page, progress_cb=progress_cb)
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                try:
                    page.mouse.wheel(0, 1600)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.8)
            try:
                dom_ids = page.evaluate(
                    """() => {
                      const out = [];
                      const add = (s) => { if (s && /^\\d{5,}$/.test(s)) out.push(s); };
                      for (const a of document.querySelectorAll('a[href]')) {
                        const href = a.href || a.getAttribute('href') || '';
                        for (const m of href.matchAll(/[?&]id=(\\d{5,})/g)) add(m[1]);
                        for (const m of href.matchAll(/\\/i(\\d{5,})\\.htm/g)) add(m[1]);
                      }
                      for (const el of document.querySelectorAll('[data-nid],[data-id],[data-itemid]')) {
                        add(el.getAttribute('data-nid')
                          || el.getAttribute('data-id')
                          || el.getAttribute('data-itemid'));
                      }
                      const html = document.documentElement
                        ? document.documentElement.innerHTML : '';
                      for (const m of html.matchAll(
                        /["'](?:itemId|item_id|itemNumId|nid|auctionId)["']\\s*[:=]\\s*["']?(\\d{5,})/g
                      )) add(m[1]);
                      return [...new Set(out)];
                    }"""
                )
            except Exception as e:  # noqa: BLE001
                _p(f"[chrome] 采链 DOM 抽取失败: {e}")
                dom_ids = []
            ids: list[str] = []
            for iid in list(dom_ids or []) + captured:
                s = str(iid).strip()
                if s.isdigit() and s not in ids:
                    ids.append(s)
            _p(f"[chrome] 采链抽到 {len(ids)} 个商品 id")
            return ids
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:  # noqa: BLE001
                pass
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass


def open_chrome_profile(url: str = "https://www.taobao.com/") -> int:
    """打开/复用抓取专用调试 Chrome，返回端口。"""
    return ensure_chrome(url=url or "https://www.taobao.com/")


def _cookie_candidates_from_profile() -> list[Path]:
    return [
        PROFILE_DIR / "Default" / "Network" / "Cookies",
        PROFILE_DIR / "Default" / "Cookies",
    ]


def read_profile_cookie_file() -> dict:
    """从抓取专用 Chrome 配置目录读淘宝 Cookie（Chrome 未占用文件时可用）。"""
    try:
        import browser_cookie3  # type: ignore
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "valid": False, "error": f"未安装 browser_cookie3: {e}"}
    last_err = ""
    for path in _cookie_candidates_from_profile():
        if not path.is_file():
            continue
        try:
            jar = browser_cookie3.chrome(cookie_file=str(path), domain_name=".taobao.com")
            parts = [f"{c.name}={c.value}" for c in jar if c.value]
            if not parts:
                last_err = f"{path.name} 无淘宝 Cookie"
                continue
            cookie = "; ".join(parts)
            return {
                "ok": True,
                "valid": True,
                "cookie": cookie,
                "source": f"profile:{path.name}",
            }
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            continue
    return {
        "ok": False,
        "valid": False,
        "error": last_err or "专用 Chrome 配置里没有可用淘宝 Cookie",
    }


def read_cookies_via_cdp(port: int | None = None) -> dict:
    """从正在运行的调试 Chrome 用 CDP/Playwright 读取 Cookie。"""
    port = port or debug_port()
    if not cdp_alive(port):
        return {"ok": False, "valid": False, "error": f"调试 Chrome :{port} 未运行"}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "valid": False, "error": f"未安装 playwright: {e}"}
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_endpoint(port))
            if not browser.contexts:
                return {"ok": False, "valid": False, "error": "Chrome 无可用上下文"}
            ctx = browser.contexts[0]
            cookies = ctx.cookies(
                [
                    "https://www.taobao.com",
                    "https://item.taobao.com",
                    "https://www.tmall.com",
                    "https://login.taobao.com",
                ]
            )
            parts = []
            for c in cookies:
                name = str(c.get("name") or "").strip()
                val = str(c.get("value") or "").strip()
                if name and val:
                    parts.append(f"{name}={val}")
            if not parts:
                return {"ok": False, "valid": False, "error": "CDP 未读到 Cookie（可能尚未登录）"}
            return {
                "ok": True,
                "valid": True,
                "cookie": "; ".join(parts),
                "source": f"cdp:{port}",
            }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "valid": False, "error": f"CDP 读 Cookie 失败: {e}"}


def cookie_looks_logged_in(cookie: str) -> bool:
    """粗判淘宝会话是否像已登录（有关键名即可，不做网络探测）。"""
    s = (cookie or "").lower()
    if not s:
        return False
    keys = ("_m_h5_tk", "cookie2", "unb=", "_tb_token_", "sgcookie", "tracknick")
    return any(k in s for k in keys)


def _slider_targets(page) -> list:
    """收集可能的滑块按钮（含 iframe）。"""
    sels = [
        "#nc_1_n1z",
        ".nc_iconfont.btn_slide",
        ".btn_slide",
        ".slidetounlock",
        "#nc_1__scale_text",
        ".nc-lang-cnt",
        "[class*='slide'] .btn_slide",
        "span.btn_slide",
    ]
    found = []
    try:
        for sel in sels:
            loc = page.locator(sel)
            if loc.count() > 0:
                found.append(loc.first)
    except Exception:  # noqa: BLE001
        pass
    try:
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            for sel in sels:
                try:
                    loc = fr.locator(sel)
                    if loc.count() > 0:
                        found.append(loc.first)
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    return found


def try_auto_slider(page, progress_cb: Callable[[str], None] | None = None) -> bool:
    """尝试自动拖淘宝滑块：优先 Playwright 鼠标，失败再用 OS mouse_util。"""
    auto = (os.environ.get("ABSOLUTE_AUTO_SLIDER") or "1").strip()
    if auto in ("0", "false", "no", "off"):
        return False
    targets = _slider_targets(page)
    if not targets:
        # 挤爆页可能只有刷新按钮
        try:
            btn = page.locator("text=点击按钮进行验证").first
            if btn.count() > 0:
                btn.click(timeout=2000)
                time.sleep(1.0)
                targets = _slider_targets(page)
        except Exception:  # noqa: BLE001
            pass
    if not targets:
        _log("[chrome] 未找到滑块节点，跳过自动拖")
        return False

    el = targets[0]
    try:
        box = el.bounding_box(timeout=3000)
    except Exception:  # noqa: BLE001
        box = None
    if not box:
        _log("[chrome] 滑块无 bounding_box")
        return False

    x1 = box["x"] + box["width"] * 0.5
    y1 = box["y"] + box["height"] * 0.5
    # 轨道通常向右拖 260~320px
    dist = max(260.0, min(340.0, box.get("width", 40) * 8))
    x2 = x1 + dist
    y2 = y1

    if progress_cb:
        progress_cb(f"自动拖滑块 ≈{int(dist)}px")
    _log(f"[chrome] auto_slider playwright ({x1:.0f},{y1:.0f})->({x2:.0f},{y2:.0f})")

    try:
        page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass

    # 1) Playwright 内鼠标（不抢系统焦点时也能拖）
    try:
        page.mouse.move(x1, y1)
        time.sleep(0.08)
        page.mouse.down()
        steps = 30
        for i in range(1, steps + 1):
            t = i / steps
            e = 0.5 - 0.5 * __import__("math").cos(3.14159265 * t)
            page.mouse.move(x1 + (x2 - x1) * e, y1 + (0.5 - abs(0.5 - t)) * 3)
            time.sleep(0.018)
        page.mouse.up()
        time.sleep(1.2)
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"[chrome] playwright 拖滑块失败: {e}，改试 OS 鼠标")

    # 2) OS 级鼠标（需要窗口可见且坐标换算到屏幕）
    try:
        from mouse_util import mouse_drag

        win = page.evaluate(
            """() => ({
              sx: window.screenX || window.screenLeft || 0,
              sy: window.screenY || window.screenTop || 0,
              ox: window.outerWidth - window.innerWidth,
              oy: window.outerHeight - window.innerHeight
            })"""
        )
        sx = float(win.get("sx") or 0) + float(win.get("ox") or 0) * 0.5
        # 粗略：chrome 标题栏约 outer-inner 高度差
        chrome_h = max(float(win.get("oy") or 80), 70)
        sy = float(win.get("sy") or 0) + chrome_h
        mouse_drag(int(sx + x1), int(sy + y1), int(sx + x2), int(sy + y2), steps=28, duration=0.85)
        time.sleep(1.2)
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"[chrome] OS 拖滑块失败: {e}")
        return False
