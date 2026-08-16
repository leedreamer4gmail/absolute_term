#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小李的电商扫描器本机客户端：窗口 UI，调动本机浏览器登录淘宝，抓详情上传云端。

用法:
  python app.py
  python app.py --cli --url "https://item.taobao.com/item.htm?id=..." --server https://host/absolute_term/api
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # absolute_term/

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from shop_store import (  # noqa: E402
    app_config_ini_paths,
    app_user_dir,
    bundle_dir,
    client_ini_path,
    cookie_file_path,
    default_log_path,
    default_output_dir as user_default_output_dir,
    default_shops_dir,
    is_auto_managed_path,
    is_junk_product_title,
    set_current_username,
    word_file_paths,
    writable_data_dir,
    uses_user_data,
)

CLIENT_DATA = writable_data_dir()
COOKIE_PATH = cookie_file_path()
DEFAULT_OUTPUT_DIR = user_default_output_dir()
CONFIG_JSON = CLIENT_DATA / "client.json"  # 旧配置；凭据以 config.ini 为准
CLIENT_INI = client_ini_path()
LOG_PATH = default_log_path()

# 必须在 import fetch_* 之前设置，且 fetch_item 也会每次动态读该环境变量
os.environ["ABSOLUTE_COOKIE_FILE"] = str(COOKIE_PATH)
os.environ["ABSOLUTE_CLIENT_LOG"] = str(LOG_PATH)

from cookie_util import pick_best_cookie, validate_cookie  # noqa: E402
from fetch_item import _session, fetch_item, load_cookie as load_fetch_cookie  # noqa: E402
from fetch_shop import (  # noqa: E402
    fetch_shop_catalog,
    parse_shop_key,
    resolve_shop_info,
)
from version import APP_TITLE, CLIENT_NAME, CLIENT_VERSION  # noqa: E402
DEFAULT_SERVER = os.environ.get(
    "ABSOLUTE_API",
    "https://leedreamer.cn/absolute_term/api",
)
DEFAULT_LOGIN_URL = "https://www.taobao.com/"
DEFAULT_ITEM_DELAY = 2.5
DEFAULT_DELAY_MIN = 2.0
DEFAULT_DELAY_MAX = 25.0
DEFAULT_BACKOFF = 1.8
DEFAULT_WIND_AFTER = 2
DEFAULT_WIND_SEC = 60.0


def keep_awake_begin() -> None:
    """防止系统休眠；抓取线程在后台/失焦时仍继续跑。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000040)
    except Exception:  # noqa: BLE001
        pass


def keep_awake_end() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception:  # noqa: BLE001
        pass


class AdaptiveDelay:
    """按挤爆频率自适应拉长间隔，平静后缓慢回落。"""

    def __init__(
        self,
        base: float = DEFAULT_ITEM_DELAY,
        min_s: float = DEFAULT_DELAY_MIN,
        max_s: float = DEFAULT_DELAY_MAX,
        factor: float = DEFAULT_BACKOFF,
    ) -> None:
        self.base = max(float(base), 0.5)
        self.min_s = max(float(min_s), 0.5)
        self.max_s = max(float(max_s), self.min_s)
        self.factor = max(float(factor), 1.05)
        self.current = min(max(self.base, self.min_s), self.max_s)
        self.wind_times: list[float] = []

    def on_wind(self) -> float:
        now = time.time()
        self.wind_times.append(now)
        self.wind_times = [t for t in self.wind_times if now - t < 180]
        self.current = min(self.current * self.factor, self.max_s)
        # 3 分钟内挤爆 ≥3 次，直接拉到上限附近
        if len(self.wind_times) >= 3:
            self.current = max(self.current, self.max_s * 0.85)
        return self.current

    def on_ok(self) -> float:
        # 成功则向基础值回落
        self.current = max(self.min_s, self.current * 0.92)
        if self.current < self.base * 1.05:
            self.current = min(self.base, self.current)
        return self.current

    def sleep(self, progress_cb=None, n: int = 0, total: int = 0) -> None:
        d = max(self.current, self.min_s)
        if progress_cb and d >= self.base * 1.2:
            progress_cb(n, total, f"自适应等待 {d:.1f}s（防挤爆）")
        time.sleep(d)


def _ensure_data() -> None:
    CLIENT_DATA.mkdir(parents=True, exist_ok=True)
    os.environ["ABSOLUTE_COOKIE_FILE"] = str(COOKIE_PATH)


def file_log(msg: str) -> None:
    """写入 %LOCALAPPDATA%/小李的电商扫描器/client.log（安装目录写不进去时也能留日志）。"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ensure_data()
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        try:
            print(line, end="", file=sys.stderr)
        except OSError:
            pass


def file_log_exc(prefix: str, exc: BaseException) -> None:
    import traceback

    file_log(f"{prefix} {exc}")
    file_log(traceback.format_exc())


def _ensure_client_ini() -> None:
    """保证本机账号 ini 存在。安装包写用户目录，不碰 _internal/config.ini。"""
    if CLIENT_INI.is_file():
        return
    CLIENT_INI.parent.mkdir(parents=True, exist_ok=True)
    stub = (
        "; 极限词本机客户端本地配置\n"
        "[cloud]\n"
        f"server = {DEFAULT_SERVER}\n"
        "username =\n"
        "password =\n"
        "token =\n"
        "\n[fetch]\n"
        f"item_delay_seconds = {DEFAULT_ITEM_DELAY}\n"
        f"wind_control_pause_after = {DEFAULT_WIND_AFTER}\n"
        f"wind_control_pause_seconds = {DEFAULT_WIND_SEC}\n"
        f"taobao_login_url = {DEFAULT_LOGIN_URL}\n"
        "chrome_slider_wait_seconds = 180\n"
        "\n[output]\n"
        f"dir = {DEFAULT_OUTPUT_DIR}\n"
        f"shops_dir = {default_shops_dir()}\n"
    )
    try:
        CLIENT_INI.write_text(stub, encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"无法写入本地配置 {CLIENT_INI}: {e}") from e
    _migrate_client_ini_from_install()


def _migrate_client_ini_from_install() -> None:
    """旧版把账号写进了 _internal/config.ini，迁到用户目录。"""
    want = CLIENT_INI.resolve()
    src_path = None
    for cand in (HERE / "config.ini", ROOT / "config.ini", bundle_dir() / "config.ini"):
        try:
            if cand.is_file() and cand.resolve() != want:
                src_path = cand
                break
        except OSError:
            continue
    if src_path is None:
        return
    src = configparser.ConfigParser()
    src.optionxform = str
    try:
        src.read(str(src_path), encoding="utf-8")
    except configparser.Error:
        return
    user = ""
    if src.has_option("cloud", "username"):
        user = (src.get("cloud", "username") or "").strip()
    token = ""
    if src.has_option("cloud", "token"):
        token = (src.get("cloud", "token") or "").strip()
    if not user and not token:
        return
    dst = configparser.ConfigParser()
    dst.optionxform = str
    dst.read(str(CLIENT_INI), encoding="utf-8")
    for section, keys in (
        ("cloud", ("server", "username", "password", "token")),
        (
            "fetch",
            (
                "item_delay_seconds", "item_delay_min_seconds", "item_delay_max_seconds",
                "wind_backoff_factor", "wind_control_pause_after", "wind_control_pause_seconds",
                "taobao_login_url", "chrome_slider_wait_seconds", "chrome_login_wait_seconds",
                "auto_slider", "fetch_limit", "analyze_limit", "harvest_count",
            ),
        ),
        (
            "layout",
            (
                "layout_version", "main_geometry", "main_sash", "left_sash",
                "cookie_height", "words_height", "log_height",
                "md_geometry", "md_sash", "problems_geometry",
                "settings_geometry", "login_geometry",
            ),
        ),
    ):
        if not src.has_section(section):
            continue
        if not dst.has_section(section):
            dst.add_section(section)
        for k in keys:
            if src.has_option(section, k):
                dst.set(section, k, src.get(section, k))
    with CLIENT_INI.open("w", encoding="utf-8") as f:
        dst.write(f)


def load_client_config() -> dict:
    """读取本地配置。云端账号/密码/token 只认 client/config.ini。"""
    _ensure_data()
    _ensure_client_ini()
    defaults = {
        "server": DEFAULT_SERVER,
        "username": "",
        "password": "",
        "token": "",
        "item_delay_seconds": DEFAULT_ITEM_DELAY,
        "item_delay_min_seconds": DEFAULT_DELAY_MIN,
        "item_delay_max_seconds": DEFAULT_DELAY_MAX,
        "wind_backoff_factor": DEFAULT_BACKOFF,
        "wind_control_pause_after": DEFAULT_WIND_AFTER,
        "wind_control_pause_seconds": DEFAULT_WIND_SEC,
        "taobao_login_url": DEFAULT_LOGIN_URL,
        "chrome_slider_wait_seconds": 180.0,
        "auto_slider": "1",
        "fetch_limit": 0,
        "analyze_limit": 0,
        "harvest_count": 5,
        "output_dir": str(DEFAULT_OUTPUT_DIR),
        "shops_dir": str(default_shops_dir()),
    }
    out = dict(defaults)
    # 兼容旧 client.json（仅补非凭据字段）
    if CONFIG_JSON.is_file():
        try:
            legacy = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            if isinstance(legacy, dict):
                for k in (
                    "server", "item_delay_seconds", "wind_control_pause_after",
                    "wind_control_pause_seconds", "taobao_login_url",
                ):
                    if legacy.get(k) not in (None, ""):
                        out[k] = legacy[k]
                if legacy.get("username") and not out.get("username"):
                    out["username"] = legacy["username"]
        except (OSError, json.JSONDecodeError):
            pass
    cp = configparser.ConfigParser()
    try:
        cp.read(CLIENT_INI, encoding="utf-8")
    except configparser.Error as e:
        raise RuntimeError(f"client/config.ini 无效: {e}") from e
    if cp.has_section("cloud"):
        for k in ("server", "username", "password", "token"):
            if cp.has_option("cloud", k):
                out[k] = cp.get("cloud", k)
    if cp.has_section("fetch"):
        for k, cast in (
            ("item_delay_seconds", float),
            ("item_delay_min_seconds", float),
            ("item_delay_max_seconds", float),
            ("wind_backoff_factor", float),
            ("wind_control_pause_after", int),
            ("wind_control_pause_seconds", float),
            ("chrome_slider_wait_seconds", float),
            ("chrome_login_wait_seconds", float),
        ):
            if cp.has_option("fetch", k):
                try:
                    out[k] = cast(cp.get("fetch", k))
                except ValueError as e:
                    raise RuntimeError(f"config.ini [fetch] {k} 无效: {e}") from e
        if cp.has_option("fetch", "taobao_login_url"):
            out["taobao_login_url"] = cp.get("fetch", "taobao_login_url")
        if cp.has_option("fetch", "auto_slider"):
            out["auto_slider"] = cp.get("fetch", "auto_slider").strip() or "1"
        if cp.has_option("fetch", "fetch_limit"):
            try:
                out["fetch_limit"] = int(cp.get("fetch", "fetch_limit"))
            except ValueError as e:
                raise RuntimeError(f"config.ini [fetch] fetch_limit 无效: {e}") from e
        if cp.has_option("fetch", "analyze_limit"):
            try:
                out["analyze_limit"] = int(cp.get("fetch", "analyze_limit"))
            except ValueError as e:
                raise RuntimeError(f"config.ini [fetch] analyze_limit 无效: {e}") from e
        if cp.has_option("fetch", "harvest_count"):
            try:
                out["harvest_count"] = int(cp.get("fetch", "harvest_count"))
            except ValueError as e:
                raise RuntimeError(f"config.ini [fetch] harvest_count 无效: {e}") from e
    if cp.has_section("output"):
        if cp.has_option("output", "dir"):
            out["output_dir"] = (cp.get("output", "dir") or "").strip()
        if cp.has_option("output", "shops_dir"):
            out["shops_dir"] = (cp.get("output", "shops_dir") or "").strip()
    set_current_username(str(out.get("username") or ""))
    if is_auto_managed_path(out.get("output_dir")):
        out["output_dir"] = str(user_default_output_dir(out.get("username")))
    if is_auto_managed_path(out.get("shops_dir")):
        out["shops_dir"] = str(default_shops_dir(out.get("username")))
    out["server"] = (out.get("server") or DEFAULT_SERVER).strip() or DEFAULT_SERVER
    return out


def save_client_config(cfg: dict) -> None:
    """写回 client/config.ini；退出登录时 password/token 应传空串。"""
    _ensure_data()
    _ensure_client_ini()
    cp = configparser.ConfigParser()
    cp.read(CLIENT_INI, encoding="utf-8")
    if not cp.has_section("cloud"):
        cp.add_section("cloud")
    if not cp.has_section("fetch"):
        cp.add_section("fetch")
    cp.set("cloud", "server", str(cfg.get("server") or DEFAULT_SERVER).strip())
    cp.set("cloud", "username", str(cfg.get("username") or "").strip())
    cp.set("cloud", "password", str(cfg.get("password") or ""))
    cp.set("cloud", "token", str(cfg.get("token") or "").strip())
    cp.set("fetch", "item_delay_seconds", str(cfg.get("item_delay_seconds", DEFAULT_ITEM_DELAY)))
    cp.set("fetch", "item_delay_min_seconds", str(cfg.get("item_delay_min_seconds", DEFAULT_DELAY_MIN)))
    cp.set("fetch", "item_delay_max_seconds", str(cfg.get("item_delay_max_seconds", DEFAULT_DELAY_MAX)))
    cp.set("fetch", "wind_backoff_factor", str(cfg.get("wind_backoff_factor", DEFAULT_BACKOFF)))
    cp.set(
        "fetch", "wind_control_pause_after",
        str(cfg.get("wind_control_pause_after", DEFAULT_WIND_AFTER)),
    )
    cp.set(
        "fetch", "wind_control_pause_seconds",
        str(cfg.get("wind_control_pause_seconds", DEFAULT_WIND_SEC)),
    )
    cp.set(
        "fetch", "taobao_login_url",
        str(cfg.get("taobao_login_url") or DEFAULT_LOGIN_URL).strip(),
    )
    cp.set(
        "fetch", "chrome_slider_wait_seconds",
        str(cfg.get("chrome_slider_wait_seconds", 180)),
    )
    cp.set(
        "fetch", "chrome_login_wait_seconds",
        str(cfg.get("chrome_login_wait_seconds", 180)),
    )
    cp.set("fetch", "auto_slider", str(cfg.get("auto_slider") or "1"))
    cp.set("fetch", "fetch_limit", str(int(cfg.get("fetch_limit") or 0)))
    cp.set("fetch", "analyze_limit", str(int(cfg.get("analyze_limit") or 0)))
    hc = int(cfg.get("harvest_count") or 5)
    if hc < 1:
        raise ValueError("harvest_count 必须 ≥ 1")
    cp.set("fetch", "harvest_count", str(hc))
    if not cp.has_section("output"):
        cp.add_section("output")
    od = str(cfg.get("output_dir") or "").strip()
    sd = str(cfg.get("shops_dir") or "").strip()
    cp.set("output", "dir", "" if is_auto_managed_path(od) else od)
    cp.set("output", "shops_dir", "" if is_auto_managed_path(sd) else sd)
    with CLIENT_INI.open("w", encoding="utf-8") as f:
        cp.write(f)


def apply_account_paths(username: str, cfg: dict | None = None) -> tuple[str, str]:
    """按登录用户切到 file/<用户名>/output 与 shops。手选的绝对路径才保留。"""
    from shop_store import set_shops_dir

    set_current_username(username)
    shops = default_shops_dir(username)
    out = user_default_output_dir(username)
    cfg = cfg or {}
    raw_shops = str(cfg.get("shops_dir") or "").strip()
    raw_out = str(cfg.get("output_dir") or "").strip()
    if raw_shops and not is_auto_managed_path(raw_shops):
        shops = Path(raw_shops)
    if raw_out and not is_auto_managed_path(raw_out):
        out = Path(raw_out)
    set_shops_dir(shops)
    return str(out), str(shops)


def save_local_cookie(cookie: str) -> None:
    _ensure_data()
    COOKIE_PATH.write_text(cookie.strip(), encoding="utf-8")
    os.environ["ABSOLUTE_COOKIE_FILE"] = str(COOKIE_PATH)


def load_local_cookie() -> str:
    os.environ["ABSOLUTE_COOKIE_FILE"] = str(COOKIE_PATH)
    if COOKIE_PATH.is_file():
        return COOKIE_PATH.read_text(encoding="utf-8").strip()
    return ""


def apply_cookie_text(text: str) -> dict:
    picked = pick_best_cookie(text, use_llm=False)
    if not picked.get("valid") or not picked.get("cookie"):
        return picked
    save_local_cookie(picked["cookie"])
    return picked


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


def read_chrome_cookie() -> dict:
    """尝试从本机 Chrome 读取淘宝 Cookie（不是 Edge）。"""
    try:
        import browser_cookie3  # type: ignore
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "valid": False, "error": f"未安装 browser_cookie3: {e}"}
    try:
        jar = browser_cookie3.chrome(domain_name=".taobao.com")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        hint = ""
        if "admin" in msg.lower() or "locked" in msg.lower() or "busy" in msg.lower():
            hint = "（Chrome 正在占用 Cookie 文件时可改用抓取专用 Chrome / CDP 自动读取。）"
        return {"ok": False, "valid": False, "error": f"读取 Chrome Cookie 失败: {msg}{hint}"}
    parts = [f"{c.name}={c.value}" for c in jar if c.value]
    if not parts:
        return {
            "ok": False,
            "valid": False,
            "error": "Chrome 中未找到淘宝 Cookie。请确认是在 Chrome（不是 Edge）里登录了淘宝。",
        }
    cookie = "; ".join(parts)
    check = validate_cookie(cookie)
    if not check.get("valid"):
        return {**check, "cookie": cookie}
    save_local_cookie(cookie)
    return {**check, "ok": True, "cookie": cookie, "source": "chrome"}


def auto_prepare_cookie(
    *,
    pasted: str = "",
    login_url: str = DEFAULT_LOGIN_URL,
    wait_seconds: float = 180,
    progress_cb=None,
    stop_flag: dict | None = None,
) -> dict:
    """开始抓取前自动准备 Cookie：粘贴框 → 本地文件 → 专用配置 → 系统 Chrome → CDP 等待登录。

    成功返回 {ok, cookie, source}；失败抛错或返回 valid=False。
    """
    from chrome_fetch import (  # noqa: WPS433
        cookie_looks_logged_in,
        ensure_chrome,
        read_cookies_via_cdp,
        read_profile_cookie_file,
    )

    def _prog(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)
        file_log(msg)

    def _accept(cookie: str, source: str) -> dict:
        cookie = (cookie or "").strip()
        if not cookie:
            return {"ok": False, "valid": False, "error": "空 Cookie"}
        check = validate_cookie(cookie)
        if not check.get("valid") and not cookie_looks_logged_in(cookie):
            return {**check, "ok": False, "cookie": cookie, "source": source}
        save_local_cookie(cookie)
        return {"ok": True, "valid": True, "cookie": cookie, "source": source}

    pasted = (pasted or "").strip()
    if pasted:
        picked = apply_cookie_text(pasted)
        if picked.get("valid") and picked.get("cookie"):
            _prog(f"Cookie 来自输入框（{len(picked['cookie'])} 字符）")
            return {**picked, "ok": True, "source": "paste"}

    local = load_local_cookie()
    if local:
        acc = _accept(local, "local")
        if acc.get("ok"):
            _prog(f"Cookie 来自本机缓存（{len(local)} 字符）")
            return acc

    r_prof = read_profile_cookie_file()
    if r_prof.get("cookie"):
        acc = _accept(r_prof["cookie"], r_prof.get("source") or "profile")
        if acc.get("ok"):
            _prog(f"Cookie 来自专用 Chrome 配置（{len(acc['cookie'])} 字符）")
            return acc

    r_sys = read_chrome_cookie()
    if r_sys.get("valid") and r_sys.get("cookie"):
        _prog(f"Cookie 来自系统 Chrome（{len(r_sys['cookie'])} 字符）")
        return {**r_sys, "ok": True}

    _prog("未找到可用 Cookie，自动打开抓取专用 Chrome，请在该窗口登录淘宝…")
    port = ensure_chrome(url=login_url or DEFAULT_LOGIN_URL)
    deadline = time.time() + max(30.0, float(wait_seconds or 180))
    last_err = ""
    while time.time() < deadline:
        if stop_flag and stop_flag.get("stop"):
            raise RuntimeError("已停止：等待登录被取消")
        r = read_cookies_via_cdp(port)
        if r.get("cookie") and cookie_looks_logged_in(r["cookie"]):
            acc = _accept(r["cookie"], r.get("source") or "cdp")
            if acc.get("ok") or cookie_looks_logged_in(acc.get("cookie") or ""):
                save_local_cookie(acc["cookie"])
                _prog(f"已检测到登录，Cookie 已自动保存（{len(acc['cookie'])} 字符）")
                return {"ok": True, "valid": True, "cookie": acc["cookie"], "source": acc.get("source") or "cdp"}
        last_err = r.get("error") or last_err or "尚未登录"
        left = int(deadline - time.time())
        if left % 15 == 0:
            _prog(f"等待淘宝登录中…剩余约 {left}s（{last_err}）")
        time.sleep(2.0)

    raise RuntimeError(
        "等待专用 Chrome 登录超时。请在弹出的抓取 Chrome 里登录淘宝后，再点「开始抓取」。"
        f"\n最后状态: {last_err or '无'}"
    )


def open_local_browser(url: str) -> str:
    """用 Chrome 打开 URL，返回实际使用的浏览器路径。"""
    url = (url or "").strip()
    if not url:
        raise ValueError("浏览器打开地址不能为空")
    chrome = find_chrome_exe()
    subprocess.Popen([chrome, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return chrome


def _api_root(server: str) -> str:
    server = (server or "").rstrip("/")
    if not server:
        raise ValueError("服务器 API 地址不能为空")
    if server.endswith("/api"):
        return server
    if server.endswith("/absolute_term") or server.endswith("/absolute"):
        return server + "/api"
    return server + "/absolute_term/api"


def api_request(server: str, method: str, path: str, body: dict | None = None,
                token: str = "", timeout: float = 60, *, _retried: bool = False) -> dict:
    root = _api_root(server)
    url = root + (path if path.startswith("/") else "/" + path)
    data = None
    headers = {"User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"HTTP {e.code}: {raw[:300]}") from e
        err = obj.get("error") or f"HTTP {e.code}"
        # 旧内存会话/服务重启遗留：有 refresh 钩子则静默重登一次再重试
        if (
            not _retried
            and e.code in (401, 403)
            and "未登录或会话已过期" in str(err)
            and _token_refresh_cb is not None
            and path not in ("/login", "/auth/login")
        ):
            new_tok = _token_refresh_cb()
            if new_tok:
                return api_request(
                    server, method, path, body, token=new_tok, timeout=timeout, _retried=True,
                )
        raise RuntimeError(err) from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(str(e)) from e


# GUI 登录后挂上：返回新 token；失败返回空串
_token_refresh_cb = None  # type: ignore[var-annotated]


def set_token_refresh(cb) -> None:
    global _token_refresh_cb
    _token_refresh_cb = cb


def login_server(server: str, username: str, password: str) -> dict:
    username = (username or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    if not password:
        raise ValueError("密码不能为空")
    # client=true：云端发不过期会话，与网页 TTL 会话分开
    res = api_request(server, "POST", "/login", {
        "username": username,
        "password": password,
        "client": True,
    })
    if not res.get("ok") or not res.get("token"):
        raise RuntimeError(res.get("error") or "登录失败：未返回 token")
    return res


def upload_cookie_to_server(server: str, token: str, cookie: str) -> dict:
    if not token:
        raise ValueError("未登录，无法上传 Cookie 到云端用户表")
    if not cookie.strip():
        raise ValueError("Cookie 为空")
    return api_request(server, "POST", "/cookie", {"cookie": cookie, "use_llm": False}, token=token)


def _extract_item_ids(url: str) -> list[str]:
    """从粘贴文本提取商品 ID；兼容残缺 query（无 https 前缀也能抠 id=）。"""
    text = (url or "").strip()
    if not text:
        return []
    ids: list[str] = []
    for m in re.finditer(r"(?:[?&]|^|[?&]amp;)id=(\d{5,})", text, re.I):
        iid = m.group(1)
        if iid not in ids:
            ids.append(iid)
    for m in re.finditer(r"(?:item\.htm\?|detail\.tmall\.com).*?[?&]id=(\d{5,})", text, re.I):
        iid = m.group(1)
        if iid not in ids:
            ids.append(iid)
    for m in re.finditer(r"/i(\d{5,})\.htm", text, re.I):
        iid = m.group(1)
        if iid not in ids:
            ids.append(iid)
    # 裸 id=xxx（用户有时只粘了 query）
    for m in re.finditer(r"(?<![A-Za-z0-9_])id=(\d{5,})", text, re.I):
        iid = m.group(1)
        if iid not in ids:
            ids.append(iid)
    return ids


def _is_wind_err(msg: str) -> bool:
    u = (msg or "").upper()
    return (
        "RGV587" in u
        or "USER_VALIDATE" in u
        or "SM:" in u
        or "风控" in (msg or "")
        or "滑块" in (msg or "")
    )


def resolve_targets(url: str = "", shop_id: str = "", user_id: str = "",
                    cookie: str = "") -> dict:
    """解析要抓的商品 ID 列表。全店列表风控时回退为仅抓当前商品。"""
    url = (url or "").strip()
    shop_id = (shop_id or "").strip()
    user_id = (user_id or "").strip()
    cookie = cookie or load_local_cookie()
    catalog_titles: dict[str, str] = {}
    meta = {"shop_id": shop_id, "user_id": user_id, "shop_name": "", "notice": ""}

    if shop_id and user_id:
        try:
            res = fetch_shop_catalog(shop_id, user_id, cookie=cookie, timeout=25)
        except (ValueError, RuntimeError) as e:
            raise ValueError(f"全店列表失败: {e}") from e
        ids = [str(it["id"]) for it in res["items"]]
        for it in res["items"]:
            if it.get("title"):
                catalog_titles[str(it["id"])] = str(it["title"])
        meta["notice"] = res.get("notice") or f"获取到 {len(ids)} 个商品"
        return {"item_ids": ids, "catalog_titles": catalog_titles, **meta}

    if not url:
        raise ValueError("请粘贴完整商品链接（需含 id=数字），不要只贴一段 m=… 参数")

    ids = _extract_item_ids(url)
    if len(ids) == 1:
        info = resolve_shop_info(ids[0], cookie=cookie)
        if "error" not in info:
            meta["shop_id"] = info["shop_id"]
            meta["user_id"] = info["user_id"]
            meta["shop_name"] = info.get("shop_name") or ""
            try:
                res = fetch_shop_catalog(
                    info["shop_id"], info["user_id"], cookie=cookie, timeout=25,
                )
                item_ids = [str(it["id"]) for it in res["items"]]
                if ids[0] not in item_ids:
                    item_ids.insert(0, ids[0])
                for it in res["items"]:
                    if it.get("title"):
                        catalog_titles[str(it["id"])] = str(it["title"])
                meta["notice"] = (
                    f"店铺「{meta['shop_name']}」"
                    + (res.get("notice") or f"获取到 {len(item_ids)} 个商品")
                )
                return {"item_ids": item_ids, "catalog_titles": catalog_titles, **meta}
            except (ValueError, RuntimeError) as e:
                err = str(e)
                file_log(f"CATALOG_FAIL fallback_single id={ids[0]} err={err[:300]}")
                if _is_wind_err(err):
                    meta["notice"] = (
                        f"店铺「{meta['shop_name']}」全店列表被淘宝滑块风控，"
                        f"已回退只抓当前商品 {ids[0]}。可稍后重试全店或更新 Cookie。"
                    )
                    return {"item_ids": ids, "catalog_titles": {}, **meta}
                raise ValueError(f"全店列表失败: {err}") from e
        return {
            "item_ids": ids,
            "catalog_titles": {},
            "notice": f"店铺解析失败，仅抓该商品: {info.get('error')}",
            **meta,
        }

    if ids:
        return {
            "item_ids": list(dict.fromkeys(ids)),
            "catalog_titles": {},
            "notice": f"商品链接模式: {len(ids)} 个",
            **meta,
        }

    # 无商品 id：只在有店铺域名时才走店铺解析，避免把 m=… 垃圾串当店铺
    if re.search(r"(taobao|tmall)\.com", url, re.I):
        try:
            key = parse_shop_key(url)
        except Exception as e:  # noqa: BLE001
            raise ValueError(
                "链接无法识别。请粘贴完整商品页地址，例如 "
                "https://item.taobao.com/item.htm?id=123456"
            ) from e
        raise ValueError(
            f"识别到店铺域名 {key}，请改贴「任一商品链接」（含 id=）"
        )
    raise ValueError(
        "链接无法识别（缺少 id=商品数字）。"
        "请从 Chrome 地址栏复制完整商品链接，不要只粘贴搜索参数。"
    )


def fetch_details(item_ids: list[str], catalog_titles: dict[str, str] | None = None,
                  delay: float = DEFAULT_ITEM_DELAY, progress_cb=None,
                  stop_flag: dict | None = None,
                  wind_pause_after: int = DEFAULT_WIND_AFTER,
                  wind_pause_sec: float = DEFAULT_WIND_SEC,
                  chrome_slider_wait: float = 180.0,
                  delay_min: float = DEFAULT_DELAY_MIN,
                  delay_max: float = DEFAULT_DELAY_MAX,
                  backoff_factor: float = DEFAULT_BACKOFF) -> list[dict]:
    """本机抓详情：先 mtop；RGV587/无标题则切 Chrome；间隔按挤爆频率自适应。"""
    catalog_titles = catalog_titles or {}
    os.environ["ABSOLUTE_COOKIE_FILE"] = str(COOKIE_PATH)
    sess = _session()
    results: list[dict] = []
    wind_streak = 0
    total = len(item_ids)
    chrome = None
    adap = AdaptiveDelay(delay, delay_min, delay_max, backoff_factor)
    keep_awake_begin()

    def _need_chrome(detail: dict) -> bool:
        if detail.get("wind_control"):
            return True
        err = (detail.get("error") or "").upper()
        if "RGV587" in err or "USER_VALIDATE" in err or "SM:" in err:
            return True
        if not (detail.get("title") or "").strip():
            return True
        if not (detail.get("detail_texts") or detail.get("detail_image_urls")
                or detail.get("main_image_urls")):
            return True
        return False

    try:
        for n, iid in enumerate(item_ids, 1):
            if stop_flag and stop_flag.get("stop"):
                break
            if progress_cb:
                progress_cb(n, total, f"抓取 {iid}（间隔≈{adap.current:.1f}s）")
            detail = fetch_item(iid, session=sess)
            title = detail.get("title") or catalog_titles.get(iid) or ""
            has_detail = bool(detail.get("detail_texts") or detail.get("detail_image_urls"))
            wind = bool(detail.get("wind_control"))

            if _need_chrome(detail):
                if progress_cb:
                    progress_cb(
                        n, total,
                        f"mtop 被风控/无详情 → Chrome 抓 {iid}（自动滑块）",
                    )
                file_log(f"CHROME_FALLBACK id={iid} err={(detail.get('error') or '')[:200]}")
                try:
                    if chrome is None:
                        from chrome_fetch import ChromeFetcher  # noqa: WPS433
                        chrome = ChromeFetcher()
                        chrome.start()
                    detail = chrome.fetch_item(
                        iid,
                        slider_wait=float(chrome_slider_wait),
                        progress_cb=lambda m: progress_cb(n, total, m) if progress_cb else None,
                    )
                    title = detail.get("title") or catalog_titles.get(iid) or title
                    has_detail = bool(
                        detail.get("detail_texts") or detail.get("detail_image_urls")
                    )
                    wind = bool(detail.get("wind_control"))
                    if load_local_cookie():
                        sess = _session()
                except Exception as e:  # noqa: BLE001
                    file_log(f"CHROME_FAIL id={iid} err={e}")
                    detail = {
                        **detail,
                        "error": (detail.get("error") or "") + f" | Chrome 抓取失败: {e}",
                        "wind_control": True,
                    }
                    wind = True

            if wind:
                wind_streak += 1
                new_d = adap.on_wind()
                file_log(f"WIND id={iid} delay->{new_d:.1f}s streak={wind_streak}")
                if wind_streak >= wind_pause_after:
                    if progress_cb:
                        progress_cb(
                            n, total,
                            f"挤爆频繁，长暂停 {wind_pause_sec:.0f}s（当前间隔 {new_d:.1f}s）",
                        )
                    time.sleep(wind_pause_sec)
                    wind_streak = 0
            else:
                wind_streak = 0
                adap.on_ok()

            if title and is_junk_product_title(title):
                file_log(f"JUNK_TITLE id={iid} title={title!r}")
                detail["error"] = (
                    (detail.get("error") or "")
                    + f" | 标题是「{title}」，不是商品页（未登录/验证页）"
                ).strip(" |")
                title = ""
                has_detail = False

            usable = bool(title and title != iid) and (
                has_detail or bool(detail.get("main_image_urls"))
            )
            row = {
                "id": iid,
                "title": title or iid,
                "detail_texts": list(detail.get("detail_texts") or []),
                "main_image_urls": list(detail.get("main_image_urls") or []),
                "detail_image_urls": list(detail.get("detail_image_urls") or []),
                "error": detail.get("error") or "",
                "wind_control": wind,
                "via": detail.get("via") or "mtop",
                "ok": usable and not (wind and not title),
            }
            if wind and not (has_detail or detail.get("main_image_urls")):
                row["ok"] = False
            if not row["ok"] and title and title != iid and not has_detail:
                row["error"] = (row["error"] + " | 无详情图文").strip(" |")
            results.append(row)
            if progress_cb:
                flag = "OK" if row["ok"] else "FAIL"
                extra = ""
                if not row["ok"] and row.get("error"):
                    extra = " | " + str(row["error"])[:120]
                via = f" via={row.get('via')}" if row.get("via") else ""
                progress_cb(n, total, f"[{flag}] {n}/{total} {title[:40] or iid}{via}{extra}")
            if n < total:
                adap.sleep(progress_cb, n, total)
    finally:
        keep_awake_end()
        if chrome is not None:
            try:
                chrome.close()
            except Exception:  # noqa: BLE001
                pass
    return results


def fetch_scan_bundle(server: str, token: str) -> dict:
    if not token:
        raise ValueError("未登录云端，无法拉取词表")
    return api_request(server, "GET", "/client/bundle", token=token, timeout=60)


def upload_scan_results(
    server: str,
    token: str,
    goods: list[dict],
    shop: dict | None = None,
    *,
    source_md: str | None = None,
) -> dict:
    """上传本机已扫完的结果与店铺源文件。失败则抛错。

    goods_tb 只收有问题的商品：只上传 problem 非空的行；本轮扫过的全部
    商品 id/标题/总数放在 shop.item_ids / shop.item_titles / shop.goods_sum
    上报，供云端算总商品数与清理「改好了」的历史问题记录。
    """
    if not token:
        raise ValueError("未登录云端，无法上传扫描结果")
    shop_body = dict(shop or {})
    if source_md is not None:
        shop_body["source_md"] = source_md
    all_ids: list[str] = []
    all_titles: dict[str, str] = {}
    goods_body: list[dict] = []
    for g in goods or []:
        iid = str(g.get("id") or g.get("tb_item_id") or "").strip()
        if not iid:
            continue
        all_ids.append(iid)
        title = str(g.get("title") or g.get("goods_name") or "").strip()
        if title and title != iid:
            all_titles[iid] = title
        problem = str(g.get("problem") or "").strip()
        if not problem:
            continue  # 屁事儿没有的不上传，不进 goods_tb
        goods_body.append({
            "tb_item_id": iid,
            "goods_name": title or iid,
            "goods_link": str(g.get("url") or g.get("goods_link") or "").strip(),
            "problem": problem,
        })
    shop_body["item_ids"] = all_ids
    shop_body["item_titles"] = all_titles
    shop_body["goods_sum"] = len(all_ids)
    if not all_ids and not str(shop_body.get("source_md") or "").strip():
        raise ValueError("没有可上传的商品结果，也没有源文件")
    body = {
        "shop": shop_body,
        "goods": goods_body,
        "client": CLIENT_NAME,
        "client_version": CLIENT_VERSION,
    }
    res = api_request(server, "POST", "/scan/results", body, token=token, timeout=180)
    if not isinstance(res, dict) or not res.get("ok"):
        raise RuntimeError(
            (res or {}).get("error") if isinstance(res, dict) else str(res)
            or "上传扫描结果失败"
        )
    return res


def fetch_shop_source_md(server: str, token: str, tb_shop_id: str) -> dict:
    sid = str(tb_shop_id or "").strip()
    if not sid:
        raise ValueError("缺少店铺 id，无法从云端拉源文件")
    if not token:
        raise ValueError("未登录云端，无法下载源文件")
    q = urllib.parse.urlencode({"shop_id": sid})
    d = api_request(server, "GET", f"/shops/source?{q}", token=token, timeout=120)
    if not isinstance(d, dict) or not d.get("ok"):
        raise RuntimeError(
            (d or {}).get("error") if isinstance(d, dict) else str(d) or "下载源文件失败"
        )
    return d


def ensure_local_shop_md(shop: dict, server: str, token: str) -> Path:
    """仅在用户看源文件/分析该店时调用：本地有 md 就用；没有才 GET /shops/source。启动不扫全量。"""
    from shop_store import match_local_shop_md, write_shop_md_text

    name = str(shop.get("shop_name") or shop.get("tb_shop_id") or shop.get("shop_id") or "").strip()
    if not name:
        raise ValueError("店铺名为空，无法定位源文件")
    local = match_local_shop_md(shop)
    if local is not None:
        return local
    tb_id = str(shop.get("tb_shop_id") or shop.get("shop_id") or "").strip()
    if not tb_id:
        raise FileNotFoundError(
            f"本地没有「{name}」的源文件，且缺少店铺 id，无法从云端下载。请重新扫描。"
        )
    d = fetch_shop_source_md(server, token, tb_id)
    text = str(d.get("source_md") or "")
    if not text.strip():
        raise FileNotFoundError(
            f"本地和云端都没有「{name}」的源文件。请对该店点「重新扫描」。"
        )
    save_name = str(d.get("shop_name") or name).strip() or name
    return write_shop_md_text(save_name, text)


def run_local_scan_and_upload(
    server: str,
    token: str,
    items: list[dict],
    shop: dict | None = None,
    *,
    do_ocr: bool = True,
    do_llm: bool = True,
    progress_cb=None,
) -> dict:
    """本机 OCR+扫描 → 本地 file/shops md（去重）→ 用户结果入库。

    不上传 shop_uniq_tb（总表以后后台从用户库提取）。
    """
    from local_scan import (
        load_local_word_groups,
        parse_bundle,
        purge_local_llm_secrets,
        scan_items_local,
    )
    from shop_store import save_shop_md_stats

    keep_awake_begin()
    try:
        purge_local_llm_secrets()
        llm_conf = None
        main_n, detail_n = 2, 6
        # 词表：只用本地 file/；云端 bundle 只取 LLM 与 OCR 张数
        groups = load_local_word_groups()
        if not any(w for _, w in groups):
            raise RuntimeError(
                f"本地词表为空：请编辑 {word_file_paths()[0]} 与 {word_file_paths()[1]}"
            )
        bundle = fetch_scan_bundle(server, token)
        _, llm_conf = parse_bundle(bundle)
        img = bundle.get("image") or {}
        main_n = int(img.get("main_ocr_count") or main_n)
        detail_n = int(img.get("detail_ocr_count") or detail_n)
        llm_meta = bundle.get("llm") or {}
        model = str(llm_meta.get("model") or (llm_conf or {}).get("model") or "")
        if do_llm and not llm_conf:
            file_log("LLM_SKIP: 云端未配置完整 LLM，本机仅做词表扫描")
            do_llm = False
        elif llm_conf:
            file_log(f"LLM_OK model={model} (memory only, not shown in UI)")
        file_log(
            f"WORDS_LOCAL limit={len(groups[0][1]) if groups else 0} "
            f"wrong={len(groups[1][1]) if len(groups) > 1 else 0}"
        )

        scanned = scan_items_local(
            items,
            groups,
            do_ocr=do_ocr,
            do_llm=do_llm,
            llm_conf=llm_conf,
            main_ocr_count=main_n,
            detail_ocr_count=detail_n,
            progress_cb=progress_cb,
        )
        llm_conf = None
        purge_local_llm_secrets()

        shop = shop or {}
        shop_name = str(shop.get("shop_name") or "").strip()
        if not shop_name:
            shop_name = next(
                (str(x.get("title") or "").strip() for x in scanned if x.get("title")),
                "",
            ) or "未命名店铺"
        shop_link = str(shop.get("shop_link") or shop.get("shop_url") or "").strip()
        if not shop_link and shop.get("shop_id"):
            shop_link = f"https://shop{shop['shop_id']}.taobao.com/"
        if not shop_link and scanned:
            shop_link = str(scanned[0].get("url") or "")

        # 本地 md + 云端 shop_tb.source_md（换机可再拉）
        source_md = ""
        try:
            st = save_shop_md_stats(shop_name, scanned, shop_link=shop_link)
            file_log(
                f"SHOP_MD {st['path']} added={st['added']} skipped={st['skipped']}"
            )
            if st.get("path") and Path(st["path"]).is_file():
                source_md = Path(st["path"]).read_text(encoding="utf-8")
            if progress_cb:
                progress_cb(
                    0, len(items),
                    f"本地店铺文档：新增 {st['added']}，重复跳过 {st['skipped']} → {st['path'].name}",
                )
        except Exception as e:  # noqa: BLE001
            file_log(f"SHOP_MD_FAIL {e}")
            if progress_cb:
                progress_cb(0, len(items), f"本地店铺 md 写入失败: {e}")

        res = upload_scan_results(
            server, token, scanned, shop=shop, source_md=source_md or None,
        )
        res["scanned"] = scanned
        res["problem_count"] = sum(1 for x in scanned if x.get("has_problem"))
        return res
    finally:
        keep_awake_end()


def shop_excel_filename(shop_name: str) -> str:
    """店铺名 → Excel 文件名（非法字符替换）。"""
    name = (shop_name or "").strip() or "未命名店铺"
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not safe:
        raise ValueError("店铺名无效，无法生成 Excel 文件名")
    return f"{safe}.xlsx"


def resolve_output_dir(cfg: dict | None = None) -> Path:
    raw = ""
    if isinstance(cfg, dict):
        raw = str(cfg.get("output_dir") or "").strip()
    if not raw:
        raw = str(DEFAULT_OUTPUT_DIR)
    path = Path(raw)
    if not path.is_absolute():
        base = app_user_dir() if uses_user_data() else ROOT
        path = (base / path).resolve()
    return path


def shop_excel_path(shop_name: str, cfg: dict | None = None) -> Path:
    return resolve_output_dir(cfg) / shop_excel_filename(shop_name)


def _app_config_ini_paths() -> list[Path]:
    """只读应用配置：_internal/config.ini，不是用户账号 ini。"""
    return app_config_ini_paths()


def _read_app_config() -> "configparser.ConfigParser":
    import configparser

    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    found = False
    for ini in _app_config_ini_paths():
        if ini.is_file():
            cfg.read(str(ini), encoding="utf-8")
            found = True
    if not found:
        tried = " ; ".join(str(p) for p in _app_config_ini_paths())
        raise FileNotFoundError(f"缺少配置文件: {tried}")
    return cfg


def _load_cloud_ui() -> dict:
    server = str(load_client_config().get("server") or DEFAULT_SERVER).strip()
    d = api_request(server, "GET", "/ui", timeout=8)
    if not isinstance(d, dict):
        raise RuntimeError("云端 /ui 返回无效")
    return d


def load_ui_url(key: str, missing_msg: str) -> str:
    """先读云端 GET /ui，再读本机完整 config.ini。"""
    try:
        cloud = _load_cloud_ui()
        url = str(cloud.get(key) or "").strip()
        if url:
            return url
    except Exception as e:  # noqa: BLE001
        file_log(f"UI_URL_CLOUD_FAIL {key} {e}")
    cfg = _read_app_config()
    url = ""
    if cfg.has_option("ui", key):
        url = (cfg.get("ui", key) or "").strip()
    if not url:
        raise RuntimeError(missing_msg)
    return url


def load_recharge_url() -> str:
    """充值页：云端 [ui] recharge_url，须能打开本站充值。"""
    return load_ui_url(
        "recharge_url",
        "未配置充值页：请在云端 config.ini [ui] recharge_url 填写地址",
    )


def load_cookie_guide_url() -> str:
    return load_ui_url(
        "cookie_guide_url",
        "未配置 Cookie 说明页：请在云端 config.ini [ui] cookie_guide_url 填写地址",
    )


def load_register_url() -> str:
    return load_ui_url(
        "register_url",
        "未配置注册页：请在云端 config.ini [ui] register_url 填写地址",
    )


# ---------- GUI ----------

def _resolve_logo_paths() -> tuple[Path, Path]:
    """从 config.ini [ui] 读 Logo 路径；缺文件则报错。"""
    cfg = _read_app_config()
    logo_rel = "file/img/logo.jpg"
    icon_rel = "file/img/logox.jpg"
    if cfg.has_option("ui", "logo_file"):
        logo_rel = (cfg.get("ui", "logo_file") or "").strip() or logo_rel
    if cfg.has_option("ui", "logo_icon_file"):
        icon_rel = (cfg.get("ui", "logo_icon_file") or "").strip() or icon_rel
    candidates_logo = [
        (bundle_dir() / logo_rel),
        (HERE / logo_rel),
        (ROOT / logo_rel),
    ]
    candidates_icon = [
        (bundle_dir() / icon_rel),
        (HERE / icon_rel),
        (ROOT / icon_rel),
    ]
    logo = next((p.resolve() for p in candidates_logo if p.is_file()), None)
    icon = next((p.resolve() for p in candidates_icon if p.is_file()), None)
    if logo is None:
        raise FileNotFoundError(f"Logo 文件不存在: {logo_rel}（config [ui] logo_file）")
    if icon is None:
        raise FileNotFoundError(f"窗口图标文件不存在: {icon_rel}（config [ui] logo_icon_file）")
    return logo, icon


def _load_tk_photo(path: Path, *, max_side: int | None = None):
    """JPG/PNG → tk PhotoImage（依赖 Pillow）。"""
    from PIL import Image, ImageTk

    im = Image.open(path)
    if max_side and max(im.size) > max_side:
        im = im.copy()
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(im)


def run_gui() -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
    except Exception as e:  # noqa: BLE001
        print(f"无 tkinter，请用 --cli 模式: {e}", file=sys.stderr)
        sys.exit(2)

    cfg = load_client_config()
    from ui_layout import (  # noqa: WPS433
        LAYOUT_READY_H,
        LAYOUT_READY_W,
        LEFT_SASH_BOTTOM_MIN,
        LEFT_SASH_MIN,
        MAIN_SASH_MIN,
        MAIN_SASH_RIGHT_MIN,
        bind_geometry_persist,
        clamp_left_sash,
        clamp_main_sash,
        debounce_save,
        default_left_sash,
        ellipsize,
        load_layout,
        split_md_products,
    )

    layout = load_layout(CLIENT_INI)
    server = (cfg.get("server") or DEFAULT_SERVER).strip()
    login_url = (cfg.get("taobao_login_url") or DEFAULT_LOGIN_URL).strip()
    stop_flag = {"stop": False}
    worker: dict = {"thread": None}

    root = tk.Tk()
    root.title(f"{APP_TITLE} v{CLIENT_VERSION}")
    try:
        root.geometry(layout.get("main_geometry") or "1100x720")
    except tk.TclError:
        root.geometry("1100x720")
    root.minsize(LAYOUT_READY_W + 40, LAYOUT_READY_H + 80)    # 暗色系：对齐云端 shared UiWindow / 公众页弹窗
    UI = {
        "bg": "#1a222c",
        "card": "#1a222c",
        "fg": "#e8eef4",
        "muted": "#a8b4c0",
        "border": "#2e3844",
        "primary": "#5b9fd4",
        "primary_hover": "#6eb0e0",
        "danger": "#b84a2a",
        "input_bg": "#121820",
        "select": "#2a3544",
        "btn": "#222a34",
        "btn_hover": "#2a3340",
    }
    root.configure(bg=UI["bg"])
    # 仅窗口小图标（logox）；版面不再放大 Logo
    try:
        _logo_path, icon_path = _resolve_logo_paths()
        _photo_icon = _load_tk_photo(icon_path)
        root.iconphoto(True, _photo_icon)
        root._logo_photos = (_photo_icon,)
    except Exception as e:  # noqa: BLE001
        print(f"加载窗口图标失败: {e}", file=sys.stderr)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    _font = ("Microsoft YaHei UI", 9)
    style.configure(".", background=UI["bg"], foreground=UI["fg"], fieldbackground=UI["input_bg"], font=_font)
    style.configure("TFrame", background=UI["bg"])
    style.configure("Card.TFrame", background=UI["card"])
    style.configure("TLabel", background=UI["bg"], foreground=UI["fg"], font=_font)
    style.configure("Muted.TLabel", background=UI["bg"], foreground=UI["muted"], font=_font)
    style.configure("TEntry", fieldbackground=UI["input_bg"], foreground=UI["fg"], insertcolor=UI["fg"])
    style.configure(
        "TButton",
        background=UI["btn"],
        foreground=UI["fg"],
        bordercolor=UI["border"],
        lightcolor=UI["btn"],
        darkcolor=UI["border"],
        padding=(7, 0),
        font=_font,
    )
    style.map(
        "TButton",
        background=[("active", UI["btn_hover"]), ("disabled", "#1a2028")],
        foreground=[("disabled", "#5c6370")],
    )
    style.configure(
        "Primary.TButton",
        background=UI["primary"],
        foreground="#0e141b",
        bordercolor=UI["primary"],
        lightcolor=UI["primary"],
        darkcolor=UI["primary"],
        padding=(7, 0),
        font=("Microsoft YaHei UI", 9, "bold"),
    )
    style.map(
        "Primary.TButton",
        background=[("active", UI["primary_hover"]), ("disabled", "#3a5060")],
        foreground=[("disabled", "#8a9aaa")],
    )
    style.configure("TPanedwindow", background=UI["bg"])
    style.configure(
        "Treeview",
        background=UI["input_bg"],
        fieldbackground=UI["input_bg"],
        foreground=UI["fg"],
        rowheight=22,
        font=_font,
    )
    style.configure(
        "Treeview.Heading",
        background=UI["btn"],
        foreground=UI["fg"],
        font=("Microsoft YaHei UI", 9, "bold"),
    )
    style.map("Treeview", background=[("selected", UI["select"])])
    # 单选钮（全自动/半自动）：背景吃进暗色底，不要白底色方块
    style.configure(
        "TRadiobutton",
        background=UI["bg"],
        foreground=UI["fg"],
        indicatorcolor=UI["input_bg"],
        indicatordarkcolor=UI["border"],
        font=_font,
    )
    style.map(
        "TRadiobutton",
        background=[("active", UI["bg"]), ("disabled", UI["bg"])],
        foreground=[("disabled", "#5c6370")],
        indicatorcolor=[("selected", UI["primary"])],
    )
    style.configure(
        "TCheckbutton",
        background=UI["bg"],
        foreground=UI["fg"],
        indicatorcolor=UI["input_bg"],
        indicatordarkcolor=UI["border"],
        font=_font,
    )
    style.map(
        "TCheckbutton",
        background=[("active", UI["bg"]), ("disabled", UI["bg"])],
        foreground=[("disabled", "#5c6370")],
        indicatorcolor=[("selected", UI["primary"])],
    )

    state: dict = {
        "items": [], "meta": {}, "token": cfg.get("token") or "",
        "server": server, "username": cfg.get("username") or "",
        "balance": None,
        "shops_data": [],
        "shop_problems": {},
        "selected_shop_idx": -1,
        "shop_row_widgets": [],
        "output_dir": str(cfg.get("output_dir") or DEFAULT_OUTPUT_DIR),
        "shops_dir": str(cfg.get("shops_dir") or default_shops_dir()),
        "auto_scan": False,
        "ui_mode": "single",
        "pending_count": 0,
        "shop_master_on": False,
        "shop_checked": set(),
        "shop_check_vars": [],
        "shop_check_widgets": [],
        "action_icons": {},
        "shop_windows": {},
    }
    try:
        from ui_icons import load_action_photos

        state["action_icons"] = load_action_photos(
            lambda p: tk.PhotoImage(file=str(p))
        )
    except Exception as e:  # noqa: BLE001
        file_log(f"ACTION_ICONS {e}")
    try:
        out_d, shops_d = apply_account_paths(state.get("username") or "", cfg)
        state["output_dir"] = out_d
        state["shops_dir"] = shops_d
    except Exception as e:  # noqa: BLE001
        file_log(f"ACCOUNT_PATHS {e}")

    def _shop_win_id(shop_or_name) -> str:
        if isinstance(shop_or_name, dict):
            return str(
                shop_or_name.get("tb_shop_id")
                or shop_or_name.get("shop_id")
                or shop_or_name.get("shop_name")
                or ""
            ).strip()
        return str(shop_or_name or "").strip()

    def _focus_shop_window(kind: str, ident: str) -> bool:
        """已打开则前置，不新建。"""
        key = f"{kind}:{ident}"
        win = (state.get("shop_windows") or {}).get(key)
        if win is None:
            return False
        try:
            if win.winfo_exists():
                win.deiconify()
                win.lift()
                win.focus_force()
                return True
        except tk.TclError:
            pass
        (state.get("shop_windows") or {}).pop(key, None)
        return False

    def _register_shop_window(kind: str, ident: str, win):
        key = f"{kind}:{ident}"
        state.setdefault("shop_windows", {})[key] = win

        def _close() -> None:
            (state.get("shop_windows") or {}).pop(key, None)
            try:
                win.destroy()
            except tk.TclError:
                pass

        win.protocol("WM_DELETE_WINDOW", _close)
        return _close

    menubar = tk.Menu(root, tearoff=0, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"], activeforeground=UI["fg"])
    menu_user = tk.Menu(menubar, tearoff=0, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"], activeforeground=UI["fg"])
    menu_mode = tk.Menu(menubar, tearoff=0, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"], activeforeground=UI["fg"])
    menu_settings = tk.Menu(menubar, tearoff=0, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"], activeforeground=UI["fg"])
    ui_mode_var = tk.StringVar(value="single")
    menu_mode.add_radiobutton(
        label="单店模式", variable=ui_mode_var, value="single",
        command=lambda: select_ui_mode("single"),
    )
    menu_mode.add_radiobutton(
        label="自动模式", variable=ui_mode_var, value="auto",
        command=lambda: select_ui_mode("auto"),
    )
    menubar.add_cascade(label="用户", menu=menu_user)
    menubar.add_cascade(label="模式", menu=menu_mode)
    menubar.add_cascade(label="设置", menu=menu_settings)
    root.config(menu=menubar)
    update_hint = {"shown": False, "remote": ""}

    paned = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
    paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    # 左：上操作+词表 / 下日志（竖向柱子可拖）；右：已扫店铺
    # minsize 防止把左栏拖没（只剩已扫店铺大空框）
    left_v = ttk.Panedwindow(paned, orient=tk.VERTICAL)
    shops_frm = ttk.Frame(paned, padding=6, style="Card.TFrame")
    paned.add(left_v, weight=2)
    paned.add(shops_frm, weight=3)
    try:
        paned.paneconfigure(left_v, minsize=MAIN_SASH_MIN)
        paned.paneconfigure(shops_frm, minsize=MAIN_SASH_RIGHT_MIN)
    except tk.TclError:
        pass

    frm = ttk.Frame(left_v, padding=6, style="Card.TFrame")
    log_wrap = ttk.Frame(left_v, padding=6, style="Card.TFrame")
    # 上栏权重大：未还原 sash 时也不要地包天（日志先占满）
    left_v.add(frm, weight=3)
    left_v.add(log_wrap, weight=1)
    try:
        left_v.paneconfigure(frm, minsize=LEFT_SASH_MIN)
        left_v.paneconfigure(log_wrap, minsize=LEFT_SASH_BOTTOM_MIN)
    except tk.TclError:
        pass

    def row_line(r: int, label: str, widget) -> None:
        ttk.Label(frm, text=label, style="TLabel").grid(row=r, column=0, sticky=tk.W)
        widget.grid(row=r, column=1, columnspan=3, sticky=tk.EW, pady=2)

    url_row = ttk.Frame(frm)
    url_row.columnconfigure(0, weight=1)
    ent_url = ttk.Entry(url_row)
    ent_url.pack(side=tk.LEFT, fill=tk.X, expand=True)
    btn_harvest = ttk.Button(url_row, text="自动获取链接")
    url_lab = ttk.Label(frm, text="商品链接", style="TLabel")
    url_lab.grid(row=0, column=0, sticky=tk.W)
    url_row.grid(row=0, column=1, columnspan=3, sticky=tk.EW, pady=2)
    URL_PLACEHOLDER = "填入要检查的店铺任意商品链接"

    def url_is_empty() -> bool:
        t = ent_url.get().strip()
        return (not t) or t == URL_PLACEHOLDER

    def url_show_placeholder() -> None:
        if not ent_url.get().strip() or ent_url.get() == URL_PLACEHOLDER:
            ent_url.delete(0, tk.END)
            ent_url.insert(0, URL_PLACEHOLDER)
            ent_url.configure(foreground=UI["muted"])

    def url_hide_placeholder(_e=None) -> None:
        if ent_url.get() == URL_PLACEHOLDER:
            ent_url.delete(0, tk.END)
            ent_url.configure(foreground=UI["fg"])

    def url_on_leave(_e=None) -> None:
        if not ent_url.get().strip():
            url_show_placeholder()

    ent_url.bind("<FocusIn>", url_hide_placeholder)
    ent_url.bind("<FocusOut>", url_on_leave)
    url_show_placeholder()

    auto_panel = ttk.Frame(frm)
    auto_kind = tk.StringVar(value="full")
    kind_row = ttk.Frame(auto_panel)
    kind_row.pack(fill=tk.X, pady=2)
    rb_full = ttk.Radiobutton(kind_row, text="全自动", variable=auto_kind, value="full")
    rb_semi = ttk.Radiobutton(kind_row, text="半自动", variable=auto_kind, value="semi")
    rb_full.pack(side=tk.LEFT)
    rb_semi.pack(side=tk.LEFT, padx=(12, 0))
    ttk.Label(kind_row, text="全自动：列表扫完后继续获取新店", style="Muted.TLabel").pack(
        side=tk.LEFT, padx=(12, 0),
    )
    semi_row = ttk.Frame(auto_panel)
    semi_row.pack(fill=tk.X, pady=2)
    ttk.Label(semi_row, text="获取数量").pack(side=tk.LEFT)
    ent_harvest_n = ttk.Entry(semi_row, width=8)
    ent_harvest_n.insert(0, str(cfg.get("harvest_count") or "5"))
    ent_harvest_n.pack(side=tk.LEFT, padx=(6, 8))
    btn_semi_fetch = ttk.Button(semi_row, text="获取店铺")
    btn_semi_fetch.pack(side=tk.LEFT)
    auto_prog = ttk.Progressbar(semi_row, length=160, mode="determinate")
    auto_prog.pack(side=tk.LEFT, padx=(8, 0), fill=tk.X, expand=True)
    scan_now_var = tk.StringVar(value="正在扫：—")
    ttk.Label(auto_panel, textvariable=scan_now_var).pack(anchor=tk.W, pady=2)
    auto_btn_row = ttk.Frame(auto_panel)
    auto_btn_row.pack(fill=tk.X, pady=2)
    btn_pending = ttk.Button(auto_btn_row, text="查看待检店铺（0）")
    btn_pending.pack(side=tk.LEFT)
    btn_scan_list = ttk.Button(auto_btn_row, text="开始扫描", style="Primary.TButton")
    btn_scan_list.pack(side=tk.LEFT, padx=(8, 0))

    def apply_auto_kind(_e=None) -> None:
        """全自动：灰掉半自动专用的「获取数量/获取店铺」；半自动：恢复可用。"""
        full = auto_kind.get() == "full"
        st = tk.DISABLED if full else tk.NORMAL
        ent_harvest_n.configure(state=st)
        btn_semi_fetch.configure(state=st)

    rb_full.configure(command=apply_auto_kind)
    rb_semi.configure(command=apply_auto_kind)
    queue_lab = ttk.Label(frm, text="待扫链接")
    queue_cell = ttk.Frame(frm)
    queue_cell.columnconfigure(0, weight=1)
    queue_tree = ttk.Treeview(
        queue_cell,
        columns=("status", "item_id", "shop"),
        show="headings",
        height=4,
        selectmode="browse",
    )
    queue_tree.heading("status", text="状态")
    queue_tree.heading("item_id", text="商品id")
    queue_tree.heading("shop", text="店铺")
    queue_tree.column("status", width=64, stretch=False)
    queue_tree.column("item_id", width=110, stretch=False)
    queue_tree.column("shop", width=160, stretch=True)
    queue_ys = ttk.Scrollbar(queue_cell, orient=tk.VERTICAL, command=queue_tree.yview)
    queue_tree.configure(yscrollcommand=queue_ys.set)
    queue_tree.grid(row=0, column=0, sticky=tk.EW)
    queue_ys.grid(row=0, column=1, sticky=tk.NS)

    lim_row = ttk.Frame(frm)
    lim_row.grid(row=2, column=1, columnspan=3, sticky=tk.EW, pady=2)
    ttk.Label(frm, text="抓取条数").grid(row=2, column=0, sticky=tk.W)
    ent_limit = ttk.Entry(lim_row, width=8)
    ent_limit.insert(0, str(cfg.get("fetch_limit") or "0"))
    ent_limit.pack(side=tk.LEFT)
    ttk.Label(lim_row, text="0=全部", style="Muted.TLabel").pack(side=tk.LEFT, padx=(6, 12))
    ttk.Label(lim_row, text="分析条数").pack(side=tk.LEFT)
    ent_analyze_limit = ttk.Entry(lim_row, width=8)
    ent_analyze_limit.insert(0, str(cfg.get("analyze_limit") or "0"))
    ent_analyze_limit.pack(side=tk.LEFT, padx=(4, 0))
    ttk.Label(lim_row, text="0=全部", style="Muted.TLabel").pack(side=tk.LEFT, padx=(6, 0))

    try:
        _cookie_h = max(2, min(8, int(layout.get("cookie_height") or 3)))
    except ValueError:
        _cookie_h = 3
    try:
        _words_h = max(4, min(20, int(layout.get("words_height") or 8)))
    except ValueError:
        _words_h = 8

    cookie_lab = ttk.Frame(frm)
    cookie_lab.grid(row=3, column=0, sticky=tk.NW)
    ttk.Label(cookie_lab, text="Cookie(可选)").pack(anchor=tk.W)
    btn_cookie_guide = ttk.Button(cookie_lab, text="如何获取cookies")
    btn_cookie_guide.pack(anchor=tk.W, pady=(4, 0))
    cookie_cell = ttk.Frame(frm)
    cookie_cell.grid(row=3, column=1, columnspan=3, sticky=tk.EW, pady=2)
    cookie_cell.columnconfigure(0, weight=1)
    txt_cookie = scrolledtext.ScrolledText(
        cookie_cell, height=_cookie_h, wrap=tk.WORD, bg=UI["input_bg"], fg=UI["fg"],
        insertbackground=UI["fg"], relief=tk.FLAT, borderwidth=0,
        highlightthickness=1, highlightbackground=UI["border"],
        font=_font,
    )
    txt_cookie.pack(fill=tk.BOTH, expand=True)

    def open_cookie_guide() -> None:
        import webbrowser
        try:
            url = load_cookie_guide_url()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("无法打开说明", str(e))
            return
        webbrowser.open(url)
        log_line(f"已打开 Cookie 获取说明：{url}")

    btn_cookie_guide.configure(command=open_cookie_guide)
    # 红框1：底部拖条改 Cookie 文本框高度
    cookie_grip = tk.Frame(
        cookie_cell, height=6, cursor="sb_v_double_arrow",
        bg=UI["border"], highlightthickness=0,
    )
    cookie_grip.pack(fill=tk.X, pady=(2, 0))
    cookie_grip.pack_propagate(False)
    _cookie_drag = {"y": 0, "h": _cookie_h}

    def _cookie_grip_down(event) -> None:
        _cookie_drag["y"] = event.y_root
        try:
            _cookie_drag["h"] = int(txt_cookie.cget("height"))
        except (tk.TclError, ValueError, TypeError):
            _cookie_drag["h"] = _cookie_h

    def _cookie_grip_move(event) -> None:
        dy = event.y_root - _cookie_drag["y"]
        # 约每 16px 一行
        lines = max(2, min(20, int(_cookie_drag["h"] + dy / 16)))
        try:
            txt_cookie.configure(height=lines)
        except tk.TclError:
            return

    def _cookie_grip_up(_event=None) -> None:
        persist_main_layout()

    cookie_grip.bind("<ButtonPress-1>", _cookie_grip_down)
    cookie_grip.bind("<B1-Motion>", _cookie_grip_move)
    cookie_grip.bind("<ButtonRelease-1>", _cookie_grip_up)
    local_ck = load_local_cookie()
    if local_ck:
        txt_cookie.insert("1.0", local_ck)

    # 词表：左右并排。给可见行高；多余空间仍给词表（row weight），上栏够高才不压扁
    abs_path, wrong_path = word_file_paths()

    def _read_word_file(path: Path) -> str:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    words_row = ttk.Frame(frm)
    words_row.grid(row=4, column=0, columnspan=4, sticky=tk.NSEW, pady=(4, 0))
    words_row.columnconfigure(0, weight=1)
    words_row.columnconfigure(1, weight=1)
    words_row.rowconfigure(0, weight=1)
    frm.rowconfigure(4, weight=1)

    abs_col = ttk.Frame(words_row)
    abs_col.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 4))
    abs_col.columnconfigure(0, weight=1)
    abs_col.rowconfigure(1, weight=1)
    abs_head = ttk.Frame(abs_col)
    abs_head.grid(row=0, column=0, sticky=tk.EW, pady=(0, 2))
    ttk.Label(abs_head, text="absolute word").pack(side=tk.LEFT)
    btn_save_abs = ttk.Button(abs_head, text="保存")
    btn_save_abs.pack(side=tk.RIGHT)
    txt_abs = scrolledtext.ScrolledText(
        abs_col, height=_words_h, wrap=tk.WORD,
        bg=UI["input_bg"], fg=UI["fg"], insertbackground=UI["fg"],
        relief=tk.FLAT, borderwidth=0, highlightthickness=1,
        highlightbackground=UI["border"], font=_font,
    )
    txt_abs.grid(row=1, column=0, sticky=tk.NSEW)
    txt_abs.insert("1.0", _read_word_file(abs_path))

    wrong_col = ttk.Frame(words_row)
    wrong_col.grid(row=0, column=1, sticky=tk.NSEW, padx=(4, 0))
    wrong_col.columnconfigure(0, weight=1)
    wrong_col.rowconfigure(1, weight=1)
    wrong_head = ttk.Frame(wrong_col)
    wrong_head.grid(row=0, column=0, sticky=tk.EW, pady=(0, 2))
    ttk.Label(wrong_head, text="wrong word").pack(side=tk.LEFT)
    btn_save_wrong = ttk.Button(wrong_head, text="保存")
    btn_save_wrong.pack(side=tk.RIGHT)
    txt_wrong = scrolledtext.ScrolledText(
        wrong_col, height=_words_h, wrap=tk.WORD,
        bg=UI["input_bg"], fg=UI["fg"], insertbackground=UI["fg"],
        relief=tk.FLAT, borderwidth=0, highlightthickness=1,
        highlightbackground=UI["border"], font=_font,
    )
    txt_wrong.grid(row=1, column=0, sticky=tk.NSEW)
    txt_wrong.insert("1.0", _read_word_file(wrong_path))

    def save_word_file(path: Path, widget: scrolledtext.ScrolledText, label: str) -> None:
        text = widget.get("1.0", "end-1c")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            messagebox.showerror("保存词表失败", f"无法写入 {path}\n{e}")
            return
        n = len([w for w in re.split(r"[\s]+", text) if w.strip()])
        # 不占版面空行：提示写进日志
        log_line(f"已保存 {label}（约 {n} 词）→ {path.name}")
        file_log(f"WORD_SAVE {path} words≈{n}")

    btn_save_abs.configure(
        command=lambda: save_word_file(abs_path, txt_abs, "absolute word")
    )
    btn_save_wrong.configure(
        command=lambda: save_word_file(wrong_path, txt_wrong, "wrong word")
    )

    # 红框2：按钮紧贴词表，不留空白撑高
    btns = ttk.Frame(frm)
    btns.grid(row=5, column=0, columnspan=4, sticky=tk.W, pady=(4, 2))
    frm.columnconfigure(1, weight=1)

    # 日志：expand 填下栏；上栏足够高时不会盖住词表
    log = scrolledtext.ScrolledText(
        log_wrap, height=1, wrap=tk.WORD, state=tk.DISABLED,
        bg=UI["input_bg"], fg=UI["fg"], insertbackground=UI["fg"],
        relief=tk.FLAT, borderwidth=0, highlightthickness=1,
        highlightbackground=UI["border"], font=("Consolas", 9),
    )
    log.pack(fill=tk.BOTH, expand=True)

    from text_menu import bind_text_context_menu
    _menu_kw = dict(bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"])
    bind_text_context_menu(ent_url, **_menu_kw)
    bind_text_context_menu(ent_limit, **_menu_kw)
    bind_text_context_menu(ent_analyze_limit, **_menu_kw)
    bind_text_context_menu(txt_cookie, **_menu_kw)
    bind_text_context_menu(txt_abs, **_menu_kw)
    bind_text_context_menu(txt_wrong, **_menu_kw)
    bind_text_context_menu(log, readonly=True, **_menu_kw)

    # 右栏：已扫店铺大列表
    shop_head = ttk.Frame(shops_frm)
    shop_head.pack(fill=tk.X, pady=(0, 4))
    shop_master_var = tk.BooleanVar(value=False)
    chk_shop_master = tk.Checkbutton(
        shop_head,
        variable=shop_master_var,
        onvalue=True,
        offvalue=False,
        bg=UI["bg"],
        fg=UI["fg"],
        activebackground=UI["bg"],
        activeforeground=UI["fg"],
        selectcolor=UI["input_bg"],
        highlightthickness=0,
        bd=0,
        relief=tk.FLAT,
        takefocus=0,
    )
    chk_shop_master.pack(side=tk.LEFT)
    ttk.Label(shop_head, text="已扫店铺").pack(side=tk.LEFT, padx=(4, 0))
    btn_rescan = ttk.Button(shop_head, text="重新扫描")
    btn_rescan.pack(side=tk.LEFT, padx=(8, 0))
    btn_analyze = ttk.Button(shop_head, text="分析店铺")
    btn_analyze.pack(side=tk.LEFT, padx=4)

    shop_box = ttk.Frame(shops_frm)
    shop_box.pack(fill=tk.BOTH, expand=True)
    shop_canvas = tk.Canvas(
        shop_box, bg=UI["input_bg"], highlightthickness=1,
        highlightbackground=UI["border"], bd=0,
    )
    shop_scroll = ttk.Scrollbar(shop_box, orient=tk.VERTICAL, command=shop_canvas.yview)
    shop_inner = ttk.Frame(shop_canvas)
    shop_inner.bind(
        "<Configure>",
        lambda e: shop_canvas.configure(scrollregion=shop_canvas.bbox("all")),
    )
    _shop_win = shop_canvas.create_window((0, 0), window=shop_inner, anchor="nw")

    def _shop_canvas_width(event) -> None:
        shop_canvas.itemconfigure(_shop_win, width=max(event.width - 4, 1))

    shop_canvas.bind("<Configure>", _shop_canvas_width)
    shop_canvas.configure(yscrollcommand=shop_scroll.set)
    shop_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    shop_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def persist_main_layout(_event=None) -> None:
        try:
            tw = int(paned.winfo_width())
            th = int(left_v.winfo_height())
            if tw < LAYOUT_READY_W or th < LAYOUT_READY_H:
                return
            geo = root.winfo_geometry()
            ms = clamp_main_sash(int(paned.sashpos(0)), tw)
            ls = clamp_left_sash(int(left_v.sashpos(0)), th)
            paned.sashpos(0, ms)
            left_v.sashpos(0, ls)
        except Exception:  # noqa: BLE001
            return
        debounce_save(
            CLIENT_INI,
            {
                "main_geometry": geo,
                "main_sash": str(ms),
                "left_sash": str(ls),
                "cookie_height": str(int(txt_cookie.cget("height") or _cookie_h)),
                "words_height": str(int(txt_abs.cget("height") or _words_h)),
            },
            key="main",
        )

    def _restore_sashes(retry: int = 0) -> None:
        try:
            tw = int(paned.winfo_width())
            th = int(left_v.winfo_height())
        except tk.TclError:
            tw, th = 0, 0
        # 窗体未映射完就 clamp 会把柱子夹成最小值并写坏 → 下次打开全封闭/地包天
        if tw < LAYOUT_READY_W or th < LAYOUT_READY_H:
            if retry < 40:
                root.after(50, lambda: _restore_sashes(retry + 1))
            return
        try:
            ms = clamp_main_sash(int(layout.get("main_sash") or 560), tw)
            paned.sashpos(0, ms)
        except (tk.TclError, ValueError):
            pass
        try:
            raw_ls = int(layout.get("left_sash") or 0)
            if raw_ls < LEFT_SASH_MIN:
                ls = default_left_sash(th)
            else:
                ls = clamp_left_sash(raw_ls, th)
            left_v.sashpos(0, ls)
        except (tk.TclError, ValueError):
            try:
                left_v.sashpos(0, default_left_sash(th))
            except (tk.TclError, ValueError):
                pass

    root.after(50, lambda: _restore_sashes(0))
    paned.bind("<ButtonRelease-1>", persist_main_layout, add="+")
    left_v.bind("<ButtonRelease-1>", persist_main_layout, add="+")
    # 只持久化窗口几何；柱子只在拖放结束写入
    bind_geometry_persist(root, CLIENT_INI, "main_geometry")

    def log_line(msg: str) -> None:
        file_log(msg)
        log.configure(state=tk.NORMAL)
        log.insert(tk.END, msg + "\n")
        log.see(tk.END)
        log.configure(state=tk.DISABLED)

    def refresh_queue_ui() -> None:
        from link_queue import STATUS_CN, list_items

        for iid in queue_tree.get_children():
            queue_tree.delete(iid)
        try:
            rows = list_items()
        except Exception as e:  # noqa: BLE001
            log_line(f"待扫表读取失败: {e}")
            return
        for it in rows:
            st = str(it.get("status") or "")
            queue_tree.insert(
                "",
                tk.END,
                values=(
                    STATUS_CN.get(st, st),
                    str(it.get("item_id") or ""),
                    str(it.get("shop_name") or it.get("shop_id") or ""),
                ),
            )

    def fill_url_from_queue_row(item_id: str) -> None:
        from link_queue import find_by_item_id, item_url

        iid = str(item_id or "").strip()
        if not iid:
            return
        row = find_by_item_id(iid)
        url = (row or {}).get("url") or item_url(iid)
        ent_url.delete(0, tk.END)
        ent_url.insert(0, url)
        ent_url.configure(foreground=UI["fg"])

    def on_queue_activate(_event=None) -> None:
        sel = queue_tree.selection()
        if not sel:
            return
        vals = queue_tree.item(sel[0], "values")
        if not vals or len(vals) < 2:
            return
        fill_url_from_queue_row(str(vals[1]))

    queue_tree.bind("<Double-1>", on_queue_activate)
    refresh_queue_ui()

    def persist_cfg(**kwargs):
        cfg2 = load_client_config()
        cfg2["server"] = state["server"]
        if "username" not in kwargs and state.get("username"):
            cfg2["username"] = state["username"]
        cfg2["taobao_login_url"] = login_url
        cfg2["output_dir"] = state.get("output_dir") or str(DEFAULT_OUTPUT_DIR)
        cfg2["shops_dir"] = state.get("shops_dir") or str(default_shops_dir())
        try:
            cfg2["fetch_limit"] = int((ent_limit.get() or "0").strip() or "0")
        except ValueError:
            cfg2["fetch_limit"] = 0
        try:
            cfg2["analyze_limit"] = int((ent_analyze_limit.get() or "0").strip() or "0")
        except ValueError:
            cfg2["analyze_limit"] = 0
        if "harvest_count" not in kwargs:
            try:
                cfg2["harvest_count"] = int(cfg2.get("harvest_count") or 5)
            except (TypeError, ValueError):
                cfg2["harvest_count"] = 5
        cfg2.update(kwargs)
        save_client_config(cfg2)
        if "output_dir" in kwargs:
            state["output_dir"] = str(kwargs["output_dir"])
        if "shops_dir" in kwargs:
            state["shops_dir"] = str(kwargs["shops_dir"])
        return cfg2

    def _update_menu_label() -> str:
        remote = (update_hint.get("remote") or "").strip()
        if remote:
            return f"检查更新（有新版本 {remote}）"
        return "检查更新"

    def show_update_menubar(remote: str) -> None:
        """菜单栏出现「有新版本」入口；用户菜单项同步改文案。"""
        ver = (remote or "").strip()
        if not ver:
            raise ValueError("新版本号为空")
        update_hint["remote"] = ver
        if not update_hint["shown"]:
            update_hint["shown"] = True
            menubar.add_command(
                label=f"有新版本 {ver}",
                command=check_for_updates,
            )
            root.config(menu=menubar)
        rebuild_user_menu()

    def rebuild_user_menu() -> None:
        menu_user.delete(0, tk.END)
        if state.get("token") and state.get("username"):
            bal = state.get("balance")
            bal_s = bal if bal is not None else "—"
            # 不用 DISABLED 项（Windows 菜单会重影发灰）
            menu_user.add_command(
                label=f"{state['username']} · 余额 {bal_s}",
                command=open_recharge,
            )
            menu_user.add_separator()
            menu_user.add_command(label="退出登录", command=do_logout)
            menu_user.add_command(label="注册（网页）", command=open_register)
            menu_user.add_command(label="提意见…", command=open_suggestion_dialog)
            menu_user.add_command(label="刷新已扫店铺", command=refresh_shops)
            menu_user.add_command(
                label=_update_menu_label(),
                command=check_for_updates,
            )
        else:
            menu_user.add_command(label="登录…", command=open_login_dialog)
            menu_user.add_command(label="注册（网页）", command=open_register)
            menu_user.add_command(label="刷新已扫店铺", command=refresh_shops)
            menu_user.add_command(
                label=_update_menu_label(),
                command=check_for_updates,
            )

    def set_login_ui(username: str = "", balance=None) -> None:
        if balance is not None:
            state["balance"] = balance
        if state.get("token") and username:
            state["username"] = username
            root.title(f"{APP_TITLE} v{CLIENT_VERSION} · {username}")
        else:
            state["balance"] = None
            root.title(f"{APP_TITLE} v{CLIENT_VERSION}")
        rebuild_user_menu()

    def open_recharge() -> None:
        import webbrowser
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        try:
            url = load_recharge_url()
            token = state.get("token") or ""
            if not token:
                raise RuntimeError("未登录，无法打开充值页")
            d = api_request(state["server"], "POST", "/auth/web-ticket", {}, token=token)
            ticket = str((d or {}).get("ticket") or "").strip()
            if not ticket:
                raise RuntimeError("云端没有返回登录票")
            parsed = urlparse(url)
            q = dict(parse_qsl(parsed.query, keep_blank_values=True))
            q["ticket"] = ticket
            url = urlunparse(parsed._replace(query=urlencode(q)))
            webbrowser.open(url)
            log_line("已打开充值页（网页已自动登录，不必再填用户名）")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("无法打开充值页", str(e))

    def open_register() -> None:
        import webbrowser

        try:
            url = load_register_url()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("无法打开注册页", str(e))
            return
        webbrowser.open(url)
        log_line(f"已打开注册页：{url}")

    _suggestion_win: tk.Toplevel | None = None

    def open_suggestion_dialog() -> None:
        """提意见：说明一行 + 文本框 + 提交，写入云端 suggestion_tb。"""
        nonlocal _suggestion_win
        if _suggestion_win is not None and _suggestion_win.winfo_exists():
            _suggestion_win.deiconify()
            _suggestion_win.lift()
            _suggestion_win.focus_force()
            return
        if not str(state.get("token") or "").strip():
            messagebox.showerror("提意见", "请先登录后再提意见")
            return

        win = tk.Toplevel(root)
        _suggestion_win = win
        win.title("提意见")
        win.configure(bg=UI["bg"])
        win.geometry("520x360")
        win.minsize(420, 260)
        win.transient(root)
        ttk.Label(
            win,
            text="你对软件建议增加什么功能，或者什么地方用着不顺手的，都可以写下来，作者都会看。",
            style="Muted.TLabel", wraplength=480, justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=10, pady=(10, 4))
        txt = tk.Text(
            win, bg=UI["input_bg"], fg=UI["fg"], insertbackground=UI["fg"],
            relief=tk.FLAT, highlightthickness=1,
            highlightbackground=UI["border"], highlightcolor=UI["primary"], undo=True,
        )
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        from text_menu import bind_text_context_menu
        bind_text_context_menu(txt, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"])
        bar = ttk.Frame(win)
        bar.pack(fill=tk.X, padx=10, pady=(2, 10))
        msg = ttk.Label(bar, text="", style="Muted.TLabel")
        msg.pack(side=tk.LEFT)
        btn = ttk.Button(bar, text="提交", style="Primary.TButton")
        btn.pack(side=tk.RIGHT)

        def _submit() -> None:
            comment = txt.get("1.0", tk.END).strip()
            if not comment:
                msg.configure(text="请先写点内容再提交")
                return
            msg.configure(text="提交中…")
            btn.configure(state=tk.DISABLED)

            def job() -> None:
                try:
                    api_request(
                        state["server"], "POST", "/suggestion",
                        {"comment": comment}, token=state["token"],
                    )

                    def _ok() -> None:
                        if not win.winfo_exists():
                            return
                        txt.delete("1.0", tk.END)
                        msg.configure(text="已提交，感谢你的意见！")
                        btn.configure(state=tk.NORMAL)

                    root.after(0, _ok)
                except Exception as e:  # noqa: BLE001
                    err = str(e)

                    def _fail() -> None:
                        if not win.winfo_exists():
                            return
                        msg.configure(text=err)
                        btn.configure(state=tk.NORMAL)

                    root.after(0, _fail)

            threading.Thread(target=job, daemon=True).start()

        btn.configure(command=_submit)
        txt.focus_set()

    def apply_ui_mode() -> None:
        auto = state.get("ui_mode") == "auto"
        if auto:
            url_lab.grid_remove()
            url_row.grid_remove()
            queue_lab.grid_remove()
            queue_cell.grid_remove()
            auto_panel.grid(row=0, column=0, columnspan=4, sticky=tk.EW, pady=2)
            try:
                btn_harvest.pack_forget()
            except tk.TclError:
                pass
            if btn_fetch.winfo_ismapped():
                btn_fetch.pack_forget()
            if btn_auto_random.winfo_ismapped():
                btn_auto_random.pack_forget()
            apply_auto_kind()
        else:
            auto_panel.grid_remove()
            url_lab.grid(row=0, column=0, sticky=tk.W)
            url_row.grid(row=0, column=1, columnspan=3, sticky=tk.EW, pady=2)
            queue_lab.grid_remove()
            queue_cell.grid_remove()
            try:
                btn_harvest.pack_forget()
            except tk.TclError:
                pass
            if not btn_fetch.winfo_ismapped():
                btn_fetch.pack(side=tk.LEFT, before=btn_stop)
            try:
                btn_auto_random.pack_forget()
            except tk.TclError:
                pass
        if auto:
            refresh_pending_label()

    def refresh_pending_label() -> None:
        n = int(state.get("pending_count") or 0)
        btn_pending.configure(text=f"查看待检店铺（{n}）")
        if not state.get("token"):
            return

        def job() -> None:
            try:
                d = api_request(state["server"], "GET", "/scan-shops", token=state["token"])
                n2 = len(d.get("shops") or [])
            except Exception:  # noqa: BLE001
                return
            state["pending_count"] = n2
            root.after(0, lambda: btn_pending.configure(text=f"查看待检店铺（{n2}）"))

        threading.Thread(target=job, daemon=True).start()

    def drop_pending_shop(*, pk: int = 0, tb_shop_id: str = "") -> None:
        if not state.get("token"):
            return
        sid = str(tb_shop_id or "").strip()
        if not pk and not sid:
            return
        try:
            api_request(
                state["server"], "POST", "/scan-shops/delete",
                {"id": int(pk or 0), "tb_shop_id": sid},
                token=state["token"],
            )
        except Exception as e:  # noqa: BLE001
            file_log(f"SCAN_SHOP_DEL_FAIL {e}")
        root.after(0, refresh_pending_label)

    def harvest_count_now() -> int:
        raw = (ent_harvest_n.get() or "").strip()
        try:
            n = int(raw) if raw else int(load_client_config().get("harvest_count") or 5)
        except ValueError as e:
            raise ValueError("每次获取店铺数必须是整数") from e
        if n < 1:
            raise ValueError("每次获取店铺数必须 ≥ 1")
        persist_cfg(harvest_count=n)
        return n

    def harvest_to_cloud(cookie: str, count: int, progress_cb) -> dict:
        from link_harvest import harvest_into_queue

        skip_ids: set[str] = set()
        skip_names: set[str] = set()
        pending = api_request(state["server"], "GET", "/scan-shops", token=state["token"])
        for s in pending.get("shops") or []:
            if s.get("tb_shop_id"):
                skip_ids.add(str(s["tb_shop_id"]).strip())
            if s.get("shop_name"):
                skip_names.add(str(s["shop_name"]).strip())
        for s in state.get("shops_data") or []:
            sid = str(s.get("tb_shop_id") or s.get("shop_id") or "").strip()
            if sid:
                skip_ids.add(sid)
            nm = str(s.get("shop_name") or "").strip()
            if nm:
                skip_names.add(nm)
        added_n = {"n": 0}

        def skip_shop(shop_id: str, shop_name: str) -> bool:
            sid = str(shop_id or "").strip()
            name = str(shop_name or "").strip()
            if sid and sid in skip_ids:
                return True
            if name and name in skip_names:
                return True
            return False

        def commit_pending(row: dict):
            r = api_request(
                state["server"], "POST", "/scan-shops/add",
                {"shops": [row]}, token=state["token"],
            )
            if r.get("errors"):
                raise RuntimeError("写入待检查库失败: " + "; ".join(r["errors"]))
            if r.get("added"):
                added_n["n"] += 1
                sid = str(row.get("tb_shop_id") or row.get("shop_id") or "").strip()
                if sid:
                    skip_ids.add(sid)
                nm = str(row.get("shop_name") or "").strip()
                if nm:
                    skip_names.add(nm)
                n = added_n["n"]
                root.after(0, lambda v=n: auto_prog.configure(value=v))
                root.after(0, refresh_pending_label)
                return True
            return False

        root.after(0, lambda: auto_prog.configure(maximum=count, value=0))
        return harvest_into_queue(
            cookie=cookie,
            shops_data=list(state.get("shops_data") or []),
            progress_cb=progress_cb,
            stop_flag=stop_flag,
            count=count,
            skip_shop=skip_shop,
            commit_pending=commit_pending,
        )

    def prompt_enable_auto_scan() -> bool:
        if state.get("auto_scan"):
            return True
        if not state.get("token"):
            messagebox.showerror("未登录", "请先登录云端")
            return False
        try:
            me = api_request(state["server"], "GET", "/me", token=state["token"])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("无法查询开通状态", str(e))
            return False
        state["auto_scan"] = bool(me.get("auto_scan"))
        state["balance"] = me.get("balance")
        if state["auto_scan"]:
            return True
        fee = float(me.get("auto_scan_fee_yuan") or 0)
        bal = float(me.get("balance") or 0)
        msg = (
            f"尚未开通自动模式。此功能扣费 {fee:.2f} 元永久打开，是否同意？"
            if fee > 0 else
            "尚未开通自动模式。当前免费开通，是否同意？"
        )
        if not messagebox.askyesno("开通自动模式", msg):
            return False
        try:
            d = api_request(state["server"], "POST", "/auto-scan/enable", {}, token=state["token"])
        except Exception as e:  # noqa: BLE001
            err = str(e)
            if "余额不足" in err:
                if messagebox.askyesno("余额不足", f"{err}\n是否打开充值页？"):
                    open_recharge()
            else:
                messagebox.showerror("开通失败", err)
            return False
        state["auto_scan"] = True
        state["balance"] = d.get("balance")
        set_login_ui(state.get("username") or "", state["balance"])
        charged = d.get("charged")
        messagebox.showinfo(
            "已开通",
            f"自动模式已永久打开。" + (f"已扣费 {float(d.get('fee_yuan') or fee):.2f} 元。" if charged else ""),
        )
        return True

    def select_ui_mode(mode: str) -> None:
        if mode not in ("single", "auto"):
            raise ValueError(f"未知模式: {mode}")
        if state.get("ui_mode") == mode:
            ui_mode_var.set(mode)
            return
        if mode == "single":
            state["ui_mode"] = "single"
            ui_mode_var.set("single")
            apply_ui_mode()
            log_line("已切到单店模式")
            return
        if not prompt_enable_auto_scan():
            ui_mode_var.set(state.get("ui_mode") or "single")
            return
        state["ui_mode"] = "auto"
        ui_mode_var.set("auto")
        apply_ui_mode()
        log_line("已切到自动模式")

    def open_pending_window() -> None:
        if not state.get("token"):
            messagebox.showerror("未登录", "请先登录云端")
            return
        if _focus_shop_window("ui", "pending"):
            return
        try:
            d = api_request(state["server"], "GET", "/scan-shops", token=state["token"])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("无法加载待检查店铺", str(e))
            return
        shops = d.get("shops") or []
        state["pending_count"] = len(shops)
        btn_pending.configure(text=f"查看待检店铺（{len(shops)}）")
        win = tk.Toplevel(root)
        win.title("待检查店铺")
        win.configure(bg=UI["bg"])
        win.geometry("720x420")
        win.transient(root)
        _register_shop_window("ui", "pending", win)
        box = ttk.Frame(win, padding=8)
        box.pack(fill=tk.BOTH, expand=True)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        tree = ttk.Treeview(box, columns=("name", "link"), show="headings", selectmode="browse")
        tree.heading("name", text="店铺名")
        tree.heading("link", text="链接")
        tree.column("name", width=180, stretch=False)
        tree.column("link", width=480, stretch=True)
        ys = ttk.Scrollbar(box, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=ys.set)
        tree.grid(row=0, column=0, sticky=tk.NSEW)
        ys.grid(row=0, column=1, sticky=tk.NS)
        for s in shops:
            tree.insert(
                "", tk.END,
                values=(s.get("shop_name") or s.get("tb_shop_id") or "", s.get("shop_link") or ""),
            )

        def download_xlsx() -> None:
            from tkinter import filedialog
            from shop_pipeline import export_pending_shops_xlsx

            if not shops:
                messagebox.showwarning("无数据", "没有待检查店铺可导出", parent=win)
                return
            path = filedialog.asksaveasfilename(
                parent=win,
                title="下载待检查店铺",
                defaultextension=".xlsx",
                filetypes=[("Excel", "*.xlsx")],
                initialfile="待检查店铺.xlsx",
            )
            if not path:
                return
            try:
                export_pending_shops_xlsx(shops, Path(path))
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("导出失败", str(e), parent=win)
                return
            messagebox.showinfo("已保存", path, parent=win)

        btn_row = ttk.Frame(box)
        btn_row.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))
        ttk.Button(btn_row, text="下载 Excel", command=download_xlsx).pack(side=tk.LEFT)

    def open_login_dialog() -> None:
        if _focus_shop_window("ui", "login"):
            return
        win = tk.Toplevel(root)
        win.title("用户登录")
        win.transient(root)
        win.resizable(True, False)
        win.configure(bg=UI["bg"])
        try:
            win.geometry(load_layout(CLIENT_INI).get("login_geometry") or "360x200")
        except tk.TclError:
            win.geometry("360x200")
        bind_geometry_persist(win, CLIENT_INI, "login_geometry")
        _register_shop_window("ui", "login", win)
        win.grab_set()
        box = ttk.Frame(win, padding=14)
        box.pack(fill=tk.BOTH, expand=True)
        ttk.Label(box, text="用户名").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        u_ent = ttk.Entry(box, width=28)
        u_ent.grid(row=0, column=1, sticky=tk.EW, pady=4)
        u_ent.insert(0, state.get("username") or load_client_config().get("username") or "")
        ttk.Label(box, text="密码").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        p_ent = ttk.Entry(box, width=28, show="*")
        p_ent.grid(row=1, column=1, sticky=tk.EW, pady=4)
        remembered = load_client_config().get("password") or ""
        if remembered:
            p_ent.insert(0, remembered)
        from text_menu import bind_text_context_menu
        bind_text_context_menu(u_ent, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"])
        bind_text_context_menu(p_ent, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"])
        msg = ttk.Label(box, text="", style="Muted.TLabel")
        msg.grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(0, 6))
        rowb = ttk.Frame(box)
        rowb.grid(row=3, column=0, columnspan=2, sticky=tk.E)

        def _ok():
            user = u_ent.get().strip()
            pwd = p_ent.get()
            if not user or not pwd:
                msg.configure(text="请填写用户名和密码")
                return
            try:
                do_login_with(user, pwd, silent=False)
                win.destroy()
            except Exception as e:  # noqa: BLE001
                msg.configure(text=str(e))

        ttk.Button(rowb, text="取消", command=win.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(rowb, text="登录", style="Primary.TButton", command=_ok).pack(side=tk.RIGHT)
        win.bind("<Return>", lambda _e: _ok())
        win.bind("<Escape>", lambda _e: win.destroy())
        u_ent.focus_set()

    def open_settings_dialog() -> None:
        from tkinter import filedialog
        from shop_store import set_shops_dir

        if _focus_shop_window("ui", "settings"):
            return
        win = tk.Toplevel(root)
        win.title("设置")
        win.transient(root)
        win.resizable(True, True)
        win.configure(bg=UI["bg"])
        try:
            win.geometry(load_layout(CLIENT_INI).get("settings_geometry") or "720x220")
        except tk.TclError:
            win.geometry("720x220")
        bind_geometry_persist(win, CLIENT_INI, "settings_geometry")
        _register_shop_window("ui", "settings", win)
        win.grab_set()
        box = ttk.Frame(win, padding=14)
        box.pack(fill=tk.BOTH, expand=True)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Excel 输出目录").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        dir_var = tk.StringVar(value=str(state.get("output_dir") or DEFAULT_OUTPUT_DIR))
        dir_ent = ttk.Entry(box, textvariable=dir_var, width=42)
        dir_ent.grid(row=0, column=1, sticky=tk.EW, pady=4)

        def _browse_out():
            p = filedialog.askdirectory(
                title="选择 Excel 输出目录",
                initialdir=dir_var.get() or str(DEFAULT_OUTPUT_DIR),
            )
            if p:
                dir_var.set(p)

        ttk.Button(box, text="浏览…", command=_browse_out).grid(row=0, column=2, padx=(6, 0), pady=4)

        ttk.Label(box, text="店铺详情路径").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        shops_default = str(default_shops_dir())
        shops_var = tk.StringVar(value=str(state.get("shops_dir") or shops_default))
        shops_ent = ttk.Entry(box, textvariable=shops_var, width=42)
        shops_ent.grid(row=1, column=1, sticky=tk.EW, pady=4)
        from text_menu import bind_text_context_menu
        bind_text_context_menu(dir_ent, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"])
        bind_text_context_menu(shops_ent, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"])

        def _browse_shops():
            p = filedialog.askdirectory(
                title="选择店铺 md 目录",
                initialdir=shops_var.get() or shops_default,
            )
            if p:
                shops_var.set(p)

        ttk.Button(box, text="浏览…", command=_browse_shops).grid(row=1, column=2, padx=(6, 0), pady=4)

        ttk.Label(box, text="每次获取店铺数").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        harvest_var = tk.StringVar(value=str(load_client_config().get("harvest_count") or 5))
        harvest_ent = ttk.Entry(box, textvariable=harvest_var, width=8)
        harvest_ent.grid(row=2, column=1, sticky=tk.W, pady=4)
        ttk.Label(box, text="半自动/全自动一次采链入队的店铺数", style="Muted.TLabel").grid(
            row=2, column=2, sticky=tk.W, padx=(6, 0), pady=4,
        )
        bind_text_context_menu(harvest_ent, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"])

        ttk.Label(
            box,
            text="Excel / 源文件默认在程序目录 file/<当前用户名>/ 下，不进 AppData。换账号自动换目录。",
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))

        rowb = ttk.Frame(box)
        rowb.grid(row=4, column=0, columnspan=3, sticky=tk.E, pady=(12, 0))

        def _save():
            out_s = (dir_var.get() or "").strip()
            shops_s = (shops_var.get() or "").strip()
            if not out_s:
                messagebox.showerror("设置无效", "Excel 输出目录不能为空", parent=win)
                return
            if not shops_s:
                messagebox.showerror("设置无效", "店铺详情路径不能为空", parent=win)
                return
            out = Path(out_s)
            shops = Path(shops_s)
            try:
                out.mkdir(parents=True, exist_ok=True)
                shops.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror("无法创建目录", str(e), parent=win)
                return
            try:
                hc = int((harvest_var.get() or "5").strip() or "5")
            except ValueError:
                messagebox.showerror("设置无效", "每次获取店铺数必须是整数", parent=win)
                return
            if hc < 1:
                messagebox.showerror("设置无效", "每次获取店铺数必须 ≥ 1", parent=win)
                return
            persist_cfg(output_dir=str(out), shops_dir=str(shops), harvest_count=hc)
            state["output_dir"] = str(out)
            state["shops_dir"] = str(shops)
            set_shops_dir(shops)
            ent_harvest_n.delete(0, tk.END)
            ent_harvest_n.insert(0, str(hc))
            refresh_shops()
            win.destroy()
            log_line(f"设置已保存：Excel={out}；店铺md={shops}")

        ttk.Button(rowb, text="取消", command=win.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(rowb, text="保存", style="Primary.TButton", command=_save).pack(side=tk.RIGHT)

    def start_update_download(info) -> None:
        """下载新版本进度窗 + 启动安装器（菜单「检查更新」与启动弹窗共用）。"""
        from app_update import download_and_launch_update

        prog = tk.Toplevel(root)
        prog.title("下载更新")
        prog.configure(bg=UI["bg"])
        prog.resizable(False, False)
        prog.transient(root)
        try:
            prog.grab_set()
        except tk.TclError:
            pass
        box = ttk.Frame(prog, padding=14)
        box.pack(fill=tk.BOTH, expand=True)
        ttk.Label(box, text=f"下载 {info.remote} …").pack(anchor=tk.W)
        status_var = tk.StringVar(value="准备中…")
        ttk.Label(box, textvariable=status_var, style="Muted.TLabel").pack(anchor=tk.W, pady=(4, 6))
        bar = ttk.Progressbar(box, length=360, mode="determinate", maximum=100)
        bar.pack(fill=tk.X)
        pct_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=pct_var, style="Muted.TLabel").pack(anchor=tk.E, pady=(4, 0))
        prog.update_idletasks()
        try:
            px = root.winfo_rootx() + max(40, (root.winfo_width() - prog.winfo_reqwidth()) // 2)
            py = root.winfo_rooty() + max(40, (root.winfo_height() - prog.winfo_reqheight()) // 2)
            prog.geometry(f"+{px}+{py}")
        except tk.TclError:
            pass

        def _on_progress(msg: str, p: float | None) -> None:
            def _ui() -> None:
                if not prog.winfo_exists():
                    return
                status_var.set(msg)
                if p is None:
                    bar.configure(mode="indeterminate")
                    try:
                        bar.start(12)
                    except tk.TclError:
                        pass
                    pct_var.set("")
                else:
                    if str(bar.cget("mode")) == "indeterminate":
                        try:
                            bar.stop()
                        except tk.TclError:
                            pass
                    bar.configure(mode="determinate", value=max(0.0, min(100.0, float(p) * 100.0)))
                    pct_var.set(f"{int(float(p) * 100)}%")

            root.after(0, _ui)

        def job():
            try:
                path = download_and_launch_update(info, progress=_on_progress)
                def _done() -> None:
                    if prog.winfo_exists():
                        prog.destroy()
                    log_line(f"已启动安装器：{path}")
                    messagebox.showinfo("更新", f"已启动安装程序：\n{path}")

                root.after(0, _done)
            except Exception as e:  # noqa: BLE001
                err = str(e)

                def _fail() -> None:
                    if prog.winfo_exists():
                        prog.destroy()
                    messagebox.showerror("更新失败", err)

                root.after(0, _fail)

        threading.Thread(target=job, daemon=True).start()

    def check_for_updates() -> None:
        from app_update import fetch_app_update_info

        try:
            info = fetch_app_update_info(state["server"])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("检查更新失败", str(e))
            return
        if not info.update_available:
            messagebox.showinfo(
                "已是最新",
                f"当前 {info.current}，云端 {info.remote}，无需更新。",
            )
            return
        show_update_menubar(info.remote)
        note = info.note or ""
        if not messagebox.askyesno(
            "发现新版本",
            f"当前 {info.current} → 云端 {info.remote}\n{note}\n\n是否下载并安装？",
        ):
            return
        start_update_download(info)

    def view_shop_md(shop: dict) -> None:
        """看源文件：左商品列表（全部+标题…）| 柱子 | 右搜索+正文高亮。"""
        from md_highlight import highlight_markdown

        name = str(shop.get("shop_name") or shop.get("tb_shop_id") or "").strip()
        if not name:
            messagebox.showerror("无法打开", "店铺名为空")
            return
        ident = _shop_win_id(shop) or name
        if _focus_shop_window("md", ident):
            return
        try:
            path = ensure_local_shop_md(shop, state["server"], state.get("token") or "")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("没有源文件", str(e))
            return
        full_text = path.read_text(encoding="utf-8")
        products = split_md_products(full_text)
        lay = load_layout(CLIENT_INI)

        win = tk.Toplevel(root)
        win.title(f"源文件 · {path.name}")
        _register_shop_window("md", ident, win)
        try:
            win.geometry(lay.get("md_geometry") or "900x600")
        except tk.TclError:
            win.geometry("900x600")
        win.configure(bg=UI["bg"])
        win.minsize(560, 360)

        tip = ttk.Frame(win, padding=(8, 6, 8, 2))
        tip.pack(fill=tk.X)
        ttk.Label(tip, text=str(path), style="Muted.TLabel").pack(side=tk.LEFT)

        split = ttk.Panedwindow(win, orient=tk.HORIZONTAL)
        split.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(split, padding=4)
        right = ttk.Frame(split, padding=4)
        split.add(left, weight=1)
        split.add(right, weight=3)

        ttk.Label(left, text="商品").pack(anchor=tk.W)
        lb_frame = ttk.Frame(left)
        lb_frame.pack(fill=tk.BOTH, expand=True)
        lb = tk.Listbox(
            lb_frame,
            bg=UI["input_bg"],
            fg=UI["fg"],
            selectbackground=UI["select"],
            selectforeground=UI["fg"],
            highlightthickness=1,
            highlightbackground=UI["border"],
            relief=tk.FLAT,
            font=_font,
            activestyle="none",
            exportselection=False,
        )
        lb_ys = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL, command=lb.yview)
        lb.configure(yscrollcommand=lb_ys.set)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lb_ys.pack(side=tk.RIGHT, fill=tk.Y)

        # index 0 = 全部；其后对应 products
        titles = [p["title"] for p in products]
        max_chars_holder = {"n": 28}

        def refill_list() -> None:
            try:
                w = max(lb.winfo_width(), 80)
                # 约 7px/字
                max_chars_holder["n"] = max(8, w // 8)
            except tk.TclError:
                pass
            n = max_chars_holder["n"]
            sel = lb.curselection()
            cur = int(sel[0]) if sel else 0
            lb.delete(0, tk.END)
            lb.insert(tk.END, "全部商品")
            for t in titles:
                lb.insert(tk.END, ellipsize(t, n))
            if 0 <= cur < lb.size():
                lb.selection_set(cur)
                lb.activate(cur)

        search_row = ttk.Frame(right)
        search_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(search_row, text="搜索").pack(side=tk.LEFT)
        ent_search = ttk.Entry(search_row)
        ent_search.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 4))
        btn_prev = ttk.Button(search_row, text="↑", width=3)
        btn_prev.pack(side=tk.LEFT, padx=(0, 2))
        btn_next = ttk.Button(search_row, text="↓", width=3)
        btn_next.pack(side=tk.LEFT, padx=(0, 6))
        search_stat = ttk.Label(search_row, text="", style="Muted.TLabel")
        search_stat.pack(side=tk.LEFT)

        body = tk.Text(
            right,
            wrap=tk.WORD,
            bg=UI["input_bg"],
            fg=UI["fg"],
            insertbackground=UI["fg"],
            relief=tk.FLAT,
            font=("Consolas", 10),
            padx=8,
            pady=8,
        )
        ys = ttk.Scrollbar(right, orient=tk.VERTICAL, command=body.yview)
        body.configure(yscrollcommand=ys.set)
        body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ys.pack(side=tk.RIGHT, fill=tk.Y)

        body.tag_configure("search_hit", background="#5a4a1a", foreground="#ffe9a0")
        body.tag_configure("search_cur", background="#8a6a18", foreground="#fff3c0")
        state_view = {"text": full_text}
        search_nav = {"hits": [], "idx": -1, "q": ""}

        def _char_count(content: str) -> int:
            return len(content or "")

        def _update_search_stat() -> None:
            chars = _char_count(state_view.get("text") or "")
            hits = search_nav["hits"]
            q = search_nav["q"]
            if not q:
                search_stat.configure(text=f"共 {chars} 字")
                return
            n = len(hits)
            if n <= 0:
                search_stat.configure(text=f"共 {chars} 字 · 0 处")
                return
            i = int(search_nav["idx"]) + 1
            search_stat.configure(text=f"共 {chars} 字 · 第 {i}/{n} 处")

        def _mark_current_hit() -> None:
            body.tag_remove("search_cur", "1.0", tk.END)
            hits = search_nav["hits"]
            idx = int(search_nav["idx"])
            if not hits or idx < 0 or idx >= len(hits):
                return
            start, end = hits[idx]
            body.tag_add("search_cur", start, end)
            body.see(start)

        def goto_search_hit(delta: int) -> None:
            hits = search_nav["hits"]
            if not hits:
                return
            was = str(body.cget("state"))
            body.configure(state=tk.NORMAL)
            n = len(hits)
            idx = int(search_nav["idx"])
            if idx < 0:
                idx = 0 if delta >= 0 else n - 1
            else:
                idx = (idx + delta) % n
            search_nav["idx"] = idx
            _mark_current_hit()
            _update_search_stat()
            if was == str(tk.DISABLED):
                body.configure(state=tk.DISABLED)

        def show_text(content: str) -> None:
            state_view["text"] = content
            body.configure(state=tk.NORMAL)
            body.delete("1.0", tk.END)
            body.insert("1.0", content)
            highlight_markdown(body)
            apply_search_highlight()
            body.configure(state=tk.DISABLED)

        def apply_search_highlight(_event=None) -> None:
            q = (ent_search.get() or "").strip()
            was = str(body.cget("state"))
            body.configure(state=tk.NORMAL)
            body.tag_remove("search_hit", "1.0", tk.END)
            body.tag_remove("search_cur", "1.0", tk.END)
            hits: list[tuple[str, str]] = []
            prev_q = search_nav["q"]
            search_nav["q"] = q
            if q:
                start = "1.0"
                while True:
                    pos = body.search(q, start, tk.END, nocase=True)
                    if not pos:
                        break
                    end = f"{pos}+{len(q)}c"
                    body.tag_add("search_hit", pos, end)
                    hits.append((pos, end))
                    start = end
            search_nav["hits"] = hits
            if hits:
                if q != prev_q or int(search_nav["idx"]) < 0 or int(search_nav["idx"]) >= len(hits):
                    search_nav["idx"] = 0
                _mark_current_hit()
            else:
                search_nav["idx"] = -1
            _update_search_stat()
            if was == str(tk.DISABLED):
                body.configure(state=tk.DISABLED)

        def on_select(_event=None) -> None:
            sel = lb.curselection()
            if not sel:
                return
            idx = int(sel[0])
            search_nav["idx"] = -1
            if idx <= 0:
                show_text(full_text)
            elif idx - 1 < len(products):
                show_text(products[idx - 1]["body"])
            else:
                show_text(full_text)

        def persist_md(_event=None) -> None:
            try:
                geo = win.winfo_geometry()
                sash = split.sashpos(0)
            except Exception:  # noqa: BLE001
                return
            debounce_save(
                CLIENT_INI,
                {"md_geometry": geo, "md_sash": str(sash)},
                key="md",
            )

        lb.bind("<<ListboxSelect>>", on_select)
        ent_search.bind("<KeyRelease>", apply_search_highlight)
        ent_search.bind("<Return>", lambda _e: goto_search_hit(1))
        ent_search.bind("<F3>", lambda _e: goto_search_hit(1))
        ent_search.bind("<Shift-F3>", lambda _e: goto_search_hit(-1))
        body.bind("<F3>", lambda _e: goto_search_hit(1))
        body.bind("<Shift-F3>", lambda _e: goto_search_hit(-1))
        btn_next.configure(command=lambda: goto_search_hit(1))
        btn_prev.configure(command=lambda: goto_search_hit(-1))
        lb.bind("<Configure>", lambda _e: refill_list(), add="+")
        split.bind("<ButtonRelease-1>", persist_md, add="+")
        bind_geometry_persist(
            win, CLIENT_INI, "md_geometry",
            get_extra=lambda: {"md_sash": str(split.sashpos(0))},
        )
        from text_menu import bind_text_context_menu
        bind_text_context_menu(ent_search, readonly=False, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"])
        bind_text_context_menu(body, readonly=True, bg=UI["btn"], fg=UI["fg"], activebackground=UI["select"])

        refill_list()
        lb.selection_set(0)
        show_text(full_text)

        def _restore_md_sash() -> None:
            try:
                split.sashpos(0, int(lay.get("md_sash") or 240))
            except (tk.TclError, ValueError):
                pass

        win.after(80, _restore_md_sash)

    def current_output_cfg() -> dict:
        return {"output_dir": state.get("output_dir") or str(DEFAULT_OUTPUT_DIR)}

    def refresh_balance() -> None:
        if not state.get("token"):
            return
        try:
            me = api_request(state["server"], "GET", "/me", token=state["token"])
            state["username"] = me.get("username") or state.get("username") or ""
            state["balance"] = me.get("balance")
            state["auto_scan"] = bool(me.get("auto_scan"))
            set_login_ui(state["username"], state["balance"])
        except Exception as e:  # noqa: BLE001
            log_line(f"刷新余额失败: {e}")

    def select_shop_idx(idx: int) -> None:
        state["selected_shop_idx"] = idx
        for i, row in enumerate(state.get("shop_row_widgets") or []):
            bg = UI["select"] if i == idx else UI["input_bg"]
            try:
                row.configure(bg=bg)
                for ch in row.winfo_children():
                    if isinstance(ch, tk.Label):
                        ch.configure(bg=bg)
                    elif isinstance(ch, tk.Button):
                        ch.configure(bg=bg, activebackground=UI["select"])
                    elif isinstance(ch, tk.Checkbutton):
                        ch.configure(bg=bg, activebackground=bg, selectcolor=bg)
            except tk.TclError:
                pass
        shops = state.get("shops_data") or []
        if 0 <= idx < len(shops):
            fill_shop_link_box(shops[idx])

    def gen_excel_for_shop(shop: dict) -> None:
        from shop_pipeline import export_problems_xlsx

        name = str(shop.get("shop_name") or shop.get("tb_shop_id") or "").strip()
        if not name:
            messagebox.showerror("无法生成", "店铺名为空")
            return
        rows = (state.get("shop_problems") or {}).get(name)
        if rows is None:
            try:
                rows = load_shop_problems_from_cloud(shop)
                cache_shop_problems(name, rows)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("无法生成 Excel", str(e))
                return
        if not rows:
            messagebox.showwarning("无数据", f"「{name}」没有问题商品可导出")
            return
        out_path = shop_excel_path(name, current_output_cfg())
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            export_problems_xlsx(rows, out_path, shop_name=name)
            log_line(f"已生成 Excel：{out_path}")
            refresh_shops()
            messagebox.showinfo("已生成", str(out_path))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("生成失败", str(e))

    def open_excel_for_shop(shop: dict) -> None:
        name = str(shop.get("shop_name") or shop.get("tb_shop_id") or "").strip()
        if not name:
            messagebox.showerror("无法打开", "店铺名为空")
            return
        path = shop_excel_path(name, current_output_cfg())
        if not path.is_file():
            messagebox.showwarning("文件不存在", f"未找到：{path}\n请先点「生成excel」")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except AttributeError:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("打开失败", str(e))

    def on_shop_master() -> None:
        on = bool(shop_master_var.get())
        state["shop_master_on"] = on
        vars_ = state.get("shop_check_vars") or []
        widgets = state.get("shop_check_widgets") or []
        n = len(state.get("shops_data") or [])
        if on:
            state["shop_checked"] = set(range(n))
            for i, var in enumerate(vars_):
                var.set(True)
        else:
            state["shop_checked"] = set()
            for var in vars_:
                var.set(False)

    def delete_scanned_shop(shop: dict) -> None:
        name = str(shop.get("shop_name") or shop.get("tb_shop_id") or shop.get("shop_id") or "").strip()
        tb_id = str(shop.get("tb_shop_id") or shop.get("shop_id") or "").strip()
        if not tb_id:
            messagebox.showerror("无法删除", "该店铺没有 tb_shop_id，无法从云端删除")
            return
        if not messagebox.askyesno("删除已扫店铺", f"从云端删除「{name or tb_id}」？\n本地 md 不会删。"):
            return
        if not state.get("token"):
            messagebox.showerror("未登录", "请先登录云端")
            return
        try:
            api_request(
                state["server"], "POST", "/shops/delete",
                {"shop_id": tb_id}, token=state["token"],
            )
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("删除失败", str(e))
            return
        log_line(f"已删除云端店铺「{name or tb_id}」")
        refresh_shops()

    def refresh_shops() -> None:
        for w in shop_inner.winfo_children():
            w.destroy()
        state["shop_row_widgets"] = []
        state["shop_check_vars"] = []
        state["shop_check_widgets"] = []
        state["shops_data"] = []
        state["selected_shop_idx"] = -1
        state["shop_checked"] = set()

        def _empty(text: str) -> None:
            ttk.Label(shop_inner, text=text, style="Muted.TLabel").pack(anchor=tk.W, padx=4, pady=6)

        if not state.get("token"):
            _empty("（登录后自动拉取已扫店铺）")
            return
        try:
            d = api_request(state["server"], "GET", "/shops", token=state["token"])
            shops = d.get("shops") or []
            if not shops:
                _empty("（暂无已扫店铺）")
                return
            state["shops_data"] = list(shops)
            master_on = bool(state.get("shop_master_on"))
            shop_master_var.set(master_on)
            if master_on:
                state["shop_checked"] = set(range(len(shops)))
            for i, s in enumerate(shops):
                name = s.get("shop_name") or s.get("tb_shop_id") or s.get("shop_id") or "?"
                bad = s.get("bad_goods_sum", 0)
                goods = s.get("goods_sum", s.get("item_count", 0))
                row = tk.Frame(shop_inner, bg=UI["input_bg"])
                row.pack(fill=tk.X, pady=1)
                state["shop_row_widgets"].append(row)
                var = tk.BooleanVar(value=master_on)
                cb = tk.Checkbutton(
                    row,
                    variable=var,
                    onvalue=True,
                    offvalue=False,
                    bg=UI["input_bg"],
                    fg=UI["fg"],
                    activebackground=UI["input_bg"],
                    activeforeground=UI["fg"],
                    selectcolor=UI["input_bg"],
                    highlightthickness=0,
                    bd=0,
                    relief=tk.FLAT,
                    takefocus=0,
                )
                cb.pack(side=tk.LEFT, padx=(2, 0))
                state["shop_check_vars"].append(var)
                state["shop_check_widgets"].append(cb)

                def _row_check(_=None, idx=i, v=var):
                    checked = state.setdefault("shop_checked", set())
                    if v.get():
                        checked.add(idx)
                        select_shop_idx(idx)
                    else:
                        checked.discard(idx)

                cb.configure(command=_row_check)
                label = tk.Label(
                    row,
                    text=f"{name}  · 商品{goods}  · 问题{bad}",
                    bg=UI["input_bg"],
                    fg=UI["fg"],
                    font=_font,
                    anchor="w",
                )
                label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=2)
                xlsx = shop_excel_path(str(name), current_output_cfg())
                from ui_icons import HoverTip

                def _icon_btn(key: str, tip: str, cmd, *, enabled: bool = True):
                    img = (state.get("action_icons") or {}).get(key)
                    if img is not None:
                        b = tk.Button(
                            row, image=img, command=cmd,
                            bd=0, highlightthickness=0, relief=tk.FLAT,
                            bg=UI["input_bg"], activebackground=UI["select"],
                            cursor="hand2", padx=3, pady=1,
                        )
                    else:
                        b = ttk.Button(row, text=tip, command=cmd)
                    b.pack(side=tk.RIGHT, padx=2, pady=1)
                    if not enabled:
                        try:
                            b.configure(state=tk.DISABLED)
                        except tk.TclError:
                            b.state(["disabled"])
                    HoverTip(b, tip, bg=UI["btn"], fg=UI["fg"])
                    return b

                _icon_btn("open", "打开 Excel", lambda shop=dict(s): open_excel_for_shop(shop), enabled=xlsx.is_file())
                _icon_btn("xlsx", "生成 Excel", lambda shop=dict(s): gen_excel_for_shop(shop))
                _icon_btn("md", "看源文件", lambda shop=dict(s): view_shop_md(shop))

                def _sel(_e=None, idx=i):
                    select_shop_idx(idx)

                def _dbl(_e=None, shop=dict(s)):
                    select_shop_idx(state["shops_data"].index(shop) if shop in state["shops_data"] else i)
                    reopen_shop_problems(shop)

                def _ctx(e, idx=i, shop=dict(s)):
                    select_shop_idx(idx)
                    m = tk.Menu(
                        root, tearoff=0, bg=UI["btn"], fg=UI["fg"],
                        activebackground=UI["select"], activeforeground=UI["fg"],
                    )
                    m.add_command(label="删除此店铺", command=lambda sh=shop: delete_scanned_shop(sh))
                    try:
                        m.tk_popup(e.x_root, e.y_root)
                    finally:
                        m.grab_release()

                row.bind("<Button-1>", _sel)
                label.bind("<Button-1>", _sel)
                row.bind("<Double-Button-1>", _dbl)
                label.bind("<Double-Button-1>", _dbl)
                row.bind("<Button-3>", _ctx)
                label.bind("<Button-3>", _ctx)
                cb.bind("<Button-3>", _ctx)
            shop_canvas.update_idletasks()
            shop_canvas.configure(scrollregion=shop_canvas.bbox("all"))
        except Exception as e:  # noqa: BLE001
            _empty(f"（拉取失败: {e}）")

    chk_shop_master.configure(command=on_shop_master)

    def on_progress(n, total, msg):
        root.after(0, lambda: log_line(f"[{n}/{total}] {msg}"))

    def maybe_sync_cookie(cookie: str) -> None:
        if not state.get("token"):
            return
        try:
            upload_cookie_to_server(state["server"], state["token"], cookie)
            log_line("Cookie 已同步到云端")
        except Exception as e:  # noqa: BLE001
            log_line(f"同步云端 Cookie 失败: {e}")

    def do_login_with(user: str, pwd: str, *, silent: bool = False) -> None:
        res = login_server(state["server"], user, pwd)
        state["token"] = res["token"]
        state["username"] = res["user"]["username"]
        state["balance"] = (res.get("user") or {}).get("balance")
        state["auto_scan"] = bool((res.get("user") or {}).get("auto_scan"))
        persist_cfg(
            token=res["token"],
            username=state["username"],
            password=pwd,
        )
        out_d, shops_d = apply_account_paths(state["username"], load_client_config())
        state["output_dir"] = out_d
        state["shops_dir"] = shops_d
        persist_cfg(output_dir=out_d, shops_dir=shops_d)
        set_login_ui(state["username"], state["balance"])
        try:
            refresh_balance()
        except Exception:  # noqa: BLE001
            pass
        log_line(f"云端登录成功: {state['username']}（已写入 {CLIENT_INI.name}）")
        refresh_shops()
        if not silent:
            bal = state.get("balance")
            bal_s = f"\n余额：{bal}" if bal is not None else ""
            messagebox.showinfo("登录成功", f"已登录 {state['username']}{bal_s}\n下次将自动登录")

    def do_login(silent: bool = False):
        cfg_now = load_client_config()
        user = (cfg_now.get("username") or state.get("username") or "").strip()
        pwd = cfg_now.get("password") or ""
        if not user or not pwd:
            if not silent:
                open_login_dialog()
            return
        try:
            do_login_with(user, pwd, silent=silent)
        except Exception as e:  # noqa: BLE001
            if silent:
                state["token"] = ""
                persist_cfg(token="")
                set_login_ui()
                log_line(f"自动登录失效，请重新登录: {e}")
            else:
                messagebox.showerror("登录失败", str(e))

    def do_logout():
        tok = state.get("token") or ""
        if tok:
            try:
                api_request(state["server"], "POST", "/logout", {}, token=tok)
            except Exception:  # noqa: BLE001
                pass
        state["token"] = ""
        state["username"] = ""
        persist_cfg(token="", password="", username="")
        out_d, shops_d = apply_account_paths("", {})
        state["output_dir"] = out_d
        state["shops_dir"] = shops_d
        set_login_ui()
        refresh_shops()
        log_line(f"已退出登录（已清空 {CLIENT_INI.name} 中的密码与 token）")

    def try_auto_login():
        cfg_now = load_client_config()
        tok = (cfg_now.get("token") or state.get("token") or "").strip()
        user = (cfg_now.get("username") or "").strip()
        pwd = cfg_now.get("password") or ""
        if tok:
            state["token"] = tok
            try:
                me = api_request(state["server"], "GET", "/me", token=tok)
                state["username"] = me.get("username") or user
                state["balance"] = me.get("balance")
                out_d, shops_d = apply_account_paths(state["username"], cfg_now)
                state["output_dir"] = out_d
                state["shops_dir"] = shops_d
                set_login_ui(state["username"], state["balance"])
                log_line(f"自动登录成功: {state['username']} · 余额 {state.get('balance')}")
                refresh_shops()
                return
            except Exception:  # noqa: BLE001
                state["token"] = ""
                persist_cfg(token="")
        if user and pwd:
            do_login(silent=True)
            return
        set_login_ui()
        refresh_shops()
        log_line("未记住登录（请菜单「用户 → 登录」）")

    menu_settings.add_command(label="设置…", command=open_settings_dialog)
    rebuild_user_menu()

    def open_browser_ui():
        """高级：手动打开抓取专用 Chrome（开始抓取也会自动打开）。"""
        try:
            from chrome_fetch import PROFILE_DIR, open_chrome_profile  # noqa: WPS433
            url = ent_url.get().strip()
            if not url.startswith("http"):
                url = login_url
            port = open_chrome_profile(url)
            log_line(f"已打开抓取专用 Chrome（CDP :{port}，配置: {PROFILE_DIR}）")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("打开失败", str(e))

    def sync_cookie_box(cookie: str) -> None:
        txt_cookie.delete("1.0", tk.END)
        if cookie:
            txt_cookie.insert("1.0", cookie)

    def selected_shop() -> dict:
        idx = int(state.get("selected_shop_idx") or -1)
        shops = state.get("shops_data") or []
        if idx < 0 or idx >= len(shops):
            raise ValueError("请先在「已扫店铺」列表中点选一家店铺")
        return dict(shops[idx])

    def shops_for_action() -> list[dict]:
        shops = state.get("shops_data") or []
        idxs = sorted(i for i in (state.get("shop_checked") or set()) if 0 <= i < len(shops))
        if idxs:
            return [dict(shops[i]) for i in idxs]
        idx = int(state.get("selected_shop_idx") or -1)
        if 0 <= idx < len(shops):
            return [dict(shops[idx])]
        raise ValueError("请先勾选或点选一家已扫店铺")

    def shop_seed_url(shop: dict) -> str:
        """缺 tb_shop_id/seller_id 时，用入口链接或已存 item_ids 拼商品 URL。"""
        link = str(shop.get("shop_link") or shop.get("shop_url") or "").strip()
        ids = _extract_item_ids(link)
        if ids:
            if link.startswith("http"):
                return link
            return f"https://item.taobao.com/item.htm?id={ids[0]}"
        raw = shop.get("item_ids") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"item_ids 不是合法 JSON: {e}") from e
        if not isinstance(raw, list):
            raise ValueError(f"item_ids 类型无效: {type(raw).__name__}")
        for x in raw:
            iid = str(x or "").strip()
            if iid.isdigit():
                return f"https://item.taobao.com/item.htm?id={iid}"
        return ""

    def fill_shop_link_box(shop: dict) -> None:
        """点选已扫店铺时，把活着的种子商品链接填进商品链接框。"""
        url = shop_seed_url(shop)
        if not url:
            return
        ent_url.delete(0, tk.END)
        ent_url.insert(0, url)
        ent_url.configure(foreground=UI["fg"])

    def resolve_rescan_targets(shop: dict) -> dict:
        name = str(shop.get("shop_name") or shop.get("tb_shop_id") or "")
        tb_id = str(shop.get("tb_shop_id") or shop.get("shop_id") or "").strip()
        seller = str(shop.get("seller_id") or "").strip()
        if tb_id and seller:
            targets = resolve_targets(shop_id=tb_id, user_id=seller)
        else:
            url = shop_seed_url(shop)
            if not url:
                raise ValueError(
                    f"「{name or tb_id or '未命名'}」没有 tb_shop_id/seller_id，"
                    "也没有可用的商品链接或 item_ids，无法重新扫描"
                )
            targets = resolve_targets(url=url)
        if not targets.get("shop_name"):
            targets["shop_name"] = name
        if not targets.get("shop_id"):
            targets["shop_id"] = tb_id
        if not targets.get("user_id"):
            targets["user_id"] = seller
        return targets

    def show_problems_window(rows: list, shop_name: str) -> None:
        """问题商品窗：对齐云端三列表（商品名 | 命中词 | 摘要；摘要可多行）。"""
        from shop_pipeline import export_problems_xlsx
        from tkinter import filedialog
        import webbrowser

        ident = (shop_name or "").strip()
        if ident and _focus_shop_window("problems", ident):
            return
        win = tk.Toplevel(root)
        win.title(f"{shop_name or '店铺'} · 问题商品")
        _close_win = None
        if ident:
            _close_win = _register_shop_window("problems", ident, win)
        try:
            win.geometry(load_layout(CLIENT_INI).get("problems_geometry") or "820x520")
        except tk.TclError:
            win.geometry("820x520")
        win.configure(bg=UI["bg"])
        win.minsize(640, 360)
        bind_geometry_persist(win, CLIENT_INI, "problems_geometry")

        top = ttk.Frame(win, padding=(12, 10, 12, 6))
        top.pack(fill=tk.X)
        ttk.Label(
            top,
            text=f"{shop_name or '店铺'} · 问题商品",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Label(top, text=f"{len(rows)} 个", style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(8, 0)
        )

        # 表头：与云端一致 商品名 | 命中词 | 摘要
        head = tk.Frame(win, bg=UI["bg"], padx=12, pady=4)
        head.pack(fill=tk.X)
        head.columnconfigure(0, weight=3, minsize=200)
        head.columnconfigure(1, weight=0, minsize=100)
        head.columnconfigure(2, weight=4, minsize=240)
        for col, text in ((0, "商品名"), (1, "命中词"), (2, "摘要")):
            tk.Label(
                head, text=text, bg=UI["bg"], fg=UI["muted"],
                font=("Microsoft YaHei UI", 9), anchor="w",
            ).grid(row=0, column=col, sticky=tk.W, padx=(0, 8) if col < 2 else 0)

        sep0 = tk.Frame(win, height=1, bg=UI["border"])
        sep0.pack(fill=tk.X, padx=12)

        box = ttk.Frame(win)
        box.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        canvas = tk.Canvas(
            box, bg=UI["card"], highlightthickness=0, bd=0,
        )
        ys = ttk.Scrollbar(box, orient=tk.VERTICAL, command=canvas.yview)
        inner = tk.Frame(canvas, bg=UI["card"])
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_w(event) -> None:
            canvas.itemconfigure(win_id, width=max(event.width - 2, 1))

        canvas.bind("<Configure>", _on_w)
        canvas.configure(yscrollcommand=ys.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ys.pack(side=tk.RIGHT, fill=tk.Y)

        def _open_link(url: str) -> None:
            if url:
                webbrowser.open(url)

        if not rows:
            tk.Label(
                inner,
                text="该店铺暂无入库的问题商品。",
                bg=UI["card"],
                fg=UI["muted"],
                font=_font,
                anchor="w",
            ).pack(anchor=tk.W, padx=12, pady=16)
        else:
            inner.columnconfigure(0, weight=3, minsize=200)
            inner.columnconfigure(1, weight=0, minsize=100)
            inner.columnconfigure(2, weight=4, minsize=240)
            for i, r in enumerate(rows):
                name = (r.get("goods_name") or "").strip() or "—"
                link = (r.get("goods_link") or "").strip()
                kws = (r.get("hit_keywords") or "").strip() or "—"
                summary = (r.get("hit_summary") or "").strip() or "—"

                name_lbl = tk.Label(
                    inner,
                    text=name,
                    bg=UI["card"],
                    fg=UI["primary"] if link else UI["fg"],
                    font=_font,
                    cursor="hand2" if link else "",
                    anchor="nw",
                    justify=tk.LEFT,
                    wraplength=260,
                )
                name_lbl.grid(row=i * 2, column=0, sticky=tk.NW, padx=(12, 8), pady=8)
                if link:
                    name_lbl.bind("<Button-1>", lambda _e, u=link: _open_link(u))

                # 命中词：红色，对齐云端
                tk.Label(
                    inner,
                    text=kws,
                    bg=UI["card"],
                    fg="#cf1322",
                    font=_font,
                    anchor="nw",
                    justify=tk.LEFT,
                    wraplength=110,
                ).grid(row=i * 2, column=1, sticky=tk.NW, padx=(0, 8), pady=8)

                # 摘要：保留换行（主图/详情页分两行），对齐云端 pre-wrap
                tk.Label(
                    inner,
                    text=summary,
                    bg=UI["card"],
                    fg=UI["muted"],
                    font=_font,
                    anchor="nw",
                    justify=tk.LEFT,
                    wraplength=400,
                ).grid(row=i * 2, column=2, sticky=tk.NW, padx=(0, 12), pady=8)

                tk.Frame(inner, height=1, bg=UI["border"]).grid(
                    row=i * 2 + 1, column=0, columnspan=3, sticky=tk.EW, padx=12,
                )

        foot = ttk.Frame(win, padding=(12, 6, 12, 10))
        foot.pack(fill=tk.X)

        def _download():
            if not rows:
                messagebox.showwarning("无数据", "没有可导出的问题商品")
                return
            path = filedialog.asksaveasfilename(
                title="保存 Excel",
                defaultextension=".xlsx",
                initialfile=f"{shop_name}_问题商品.xlsx",
                filetypes=[("Excel", "*.xlsx")],
            )
            if not path:
                return
            try:
                out = export_problems_xlsx(rows, Path(path), shop_name=shop_name)
                messagebox.showinfo("已保存", str(out))
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("导出失败", str(e))

        ttk.Button(foot, text="下载 Excel", command=_download).pack(side=tk.LEFT)
        ttk.Button(
            foot, text="关闭", command=(_close_win or win.destroy),
        ).pack(side=tk.RIGHT)

    def cache_shop_problems(shop_name: str, rows: list) -> None:
        key = (shop_name or "").strip()
        if not key:
            return
        state.setdefault("shop_problems", {})[key] = list(rows or [])

    def load_shop_problems_from_cloud(shop: dict) -> list:
        """从云端 goods/list 拉问题商品，转成弹窗行。"""
        tb_id = str(shop.get("tb_shop_id") or shop.get("shop_id") or "").strip()
        if not tb_id:
            raise ValueError("店铺缺少 tb_shop_id，无法从云端加载问题列表")
        if not state.get("token"):
            raise ValueError("请先登录云端")
        d = api_request(
            state["server"],
            "GET",
            f"/goods/list?shop_id={urllib.parse.quote(tb_id)}",
            token=state["token"],
        )
        if d.get("error"):
            raise RuntimeError(d["error"])
        rows = []
        for g in d.get("goods") or []:
            if not ((g.get("problem") or "").strip() or g.get("has_problem")):
                continue
            kws = g.get("hit_keywords") or []
            if isinstance(kws, list):
                kws_s = "、".join(str(x) for x in kws if str(x).strip()) or "—"
            else:
                kws_s = str(kws).strip() or "—"
            rows.append({
                "goods_name": g.get("goods_name") or g.get("tb_item_id") or "",
                "goods_link": g.get("goods_link") or "",
                "hit_keywords": kws_s,
                "hit_summary": (g.get("hit_summary") or "").strip() or "—",
                "tb_item_id": g.get("tb_item_id") or "",
                "problem": g.get("problem") or "",
            })
        return rows

    def reopen_shop_problems(shop: dict | None = None) -> None:
        """双击店铺：再弹出分析结果窗（优先本地缓存，否则读云端）。"""
        try:
            shop = shop or selected_shop()
        except ValueError as e:
            messagebox.showerror("无法打开", str(e))
            return
        name = str(shop.get("shop_name") or shop.get("tb_shop_id") or "").strip()
        if not name:
            messagebox.showerror("无法打开", "店铺名为空")
            return
        ident = _shop_win_id(shop) or name
        if _focus_shop_window("problems", ident) or _focus_shop_window("problems", name):
            return
        loading = state.setdefault("shop_window_loading", set())
        load_key = f"problems:{name}"
        if load_key in loading:
            return
        cached = (state.get("shop_problems") or {}).get(name)
        if cached is not None:
            show_problems_window(cached, name)
            return

        def job():
            try:
                rows = load_shop_problems_from_cloud(shop)
                root.after(0, lambda: cache_shop_problems(name, rows))
                root.after(0, lambda: show_problems_window(rows, name))
            except Exception as e:  # noqa: BLE001
                err = str(e)
                root.after(0, lambda: messagebox.showerror("加载失败", err))
            finally:
                root.after(0, lambda k=load_key: loading.discard(k))

        loading.add(load_key)
        threading.Thread(target=job, daemon=True).start()

    def _llm_from_bundle():
        from local_scan import parse_bundle
        bundle = fetch_scan_bundle(state["server"], state["token"])
        _, llm_conf = parse_bundle(bundle)
        img = bundle.get("image") or {}
        return llm_conf, int(img.get("main_ocr_count") or 2), int(img.get("detail_ocr_count") or 6)

    def analyze_shop_sync(shop: dict, *, popup: bool = True) -> dict:
        """同步：词表/LLM 分析 + 入库。须在工作线程调用。"""
        from shop_pipeline import module_analyze_shop_md

        name = str(shop.get("shop_name") or shop.get("tb_shop_id") or "").strip()
        if not name:
            raise ValueError("店铺名为空，无法分析")
        if not state.get("token"):
            raise ValueError("请先登录云端")
        try:
            analyze_limit = int((ent_analyze_limit.get() or "0").strip() or "0")
        except ValueError as e:
            raise ValueError("分析条数必须是整数（0=全部）") from e
        if analyze_limit < 0:
            raise ValueError("分析条数不能为负数")
        persist_cfg(analyze_limit=analyze_limit)

        ensure_local_shop_md(shop, state["server"], state.get("token") or "")

        def _p(n, total, msg):
            root.after(0, lambda m=msg, a=n, b=total: log_line(f"[{a}/{b}] {m}" if b else m))

        llm_conf, _, _ = _llm_from_bundle()
        if analyze_limit:
            root.after(0, lambda n=analyze_limit: log_line(f"分析条数限制：前 {n} 个"))
        an = module_analyze_shop_md(
            name,
            llm_conf=llm_conf,
            do_llm=True,
            max_items=analyze_limit,
            progress_cb=_p,
        )
        meta = {
            "shop_id": shop.get("tb_shop_id") or shop.get("shop_id") or "",
            "user_id": shop.get("seller_id") or shop.get("user_id") or "",
            "shop_name": an.get("shop_name") or name,
            "shop_link": shop.get("shop_link") or an.get("shop_link") or "",
        }
        if not meta["user_id"] and shop.get("seller_id"):
            meta["user_id"] = shop.get("seller_id")
        upload_res = upload_scan_results(
            state["server"], state["token"], an["scanned"], shop=meta,
            source_md=Path(an["path"]).read_text(encoding="utf-8") if an.get("path") else None,
        )
        if not upload_res.get("ok"):
            raise RuntimeError(
                upload_res.get("error") or f"上传失败: {upload_res}"
            )
        goods_n = upload_res.get("goods_sum") or upload_res.get("upserted") or 0
        bad_n = upload_res.get("bad_goods_sum") or 0
        rows = an.get("problems") or []
        root.after(0, lambda: cache_shop_problems(name, rows))
        root.after(0, lambda: log_line(
            f"分析完成「{name}」：问题 {len(rows)} 个，已入库 "
            f"（库内商品 {goods_n}，问题 {bad_n}）→ 网页可查看"
        ))
        try:
            d = api_request(state["server"], "GET", "/shops", token=state["token"])
            state["shops_data"] = list(d.get("shops") or [])
        except Exception as e:  # noqa: BLE001
            file_log(f"PULL_SHOPS {e}")
        root.after(0, refresh_shops)
        root.after(0, refresh_balance)
        if popup:
            root.after(0, lambda: show_problems_window(rows, name))
        return {
            "name": name,
            "problems": len(rows),
            "goods_n": goods_n,
            "bad_n": bad_n,
        }

    def run_analyze_shop_job(shop: dict, *, after_scan: bool = False):
        """分析模块 + 入库 + 弹窗（另开工作线程）。"""
        del after_scan  # 兼容旧调用

        def job():
            try:
                analyze_shop_sync(shop, popup=True)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                file_log(f"ANALYZE_FAIL {err}")
                root.after(0, lambda: messagebox.showerror("分析失败", err))

        t = threading.Thread(target=job, daemon=True)
        worker["thread"] = t
        t.start()

    def do_rescan_selected():
        if worker.get("thread") and worker["thread"].is_alive():
            messagebox.showwarning("进行中", "已有任务在跑")
            return
        try:
            shops = shops_for_action()
        except ValueError as e:
            messagebox.showerror("无法重新扫描", str(e))
            return
        if not state.get("token"):
            messagebox.showerror("未登录", "请先登录云端")
            return
        stop_flag["stop"] = False
        cfg2 = persist_cfg(token=state.get("token") or "")
        delay = float(cfg2.get("item_delay_seconds") or DEFAULT_ITEM_DELAY)
        delay_min = float(cfg2.get("item_delay_min_seconds") or DEFAULT_DELAY_MIN)
        delay_max = float(cfg2.get("item_delay_max_seconds") or DEFAULT_DELAY_MAX)
        backoff = float(cfg2.get("wind_backoff_factor") or DEFAULT_BACKOFF)
        wind_after = int(cfg2.get("wind_control_pause_after") or DEFAULT_WIND_AFTER)
        wind_sec = float(cfg2.get("wind_control_pause_seconds") or DEFAULT_WIND_SEC)
        slider_wait = float(cfg2.get("chrome_slider_wait_seconds") or 180)
        login_wait = float(cfg2.get("chrome_login_wait_seconds") or 180)
        os.environ["ABSOLUTE_AUTO_SLIDER"] = str(cfg2.get("auto_slider") or "1")
        try:
            fetch_limit = int((ent_limit.get() or "0").strip() or "0")
        except ValueError:
            fetch_limit = 0
        pasted = txt_cookie.get("1.0", tk.END).strip()

        def job():
            from shop_pipeline import module_scan_save_md

            def _pc(msg: str) -> None:
                root.after(0, lambda m=msg: log_line(m))

            done_n = 0
            fail_n = 0
            last_path = ""
            try:
                prep = auto_prepare_cookie(
                    pasted=pasted, login_url=login_url, wait_seconds=login_wait,
                    progress_cb=_pc, stop_flag=stop_flag,
                )
                if not prep.get("cookie"):
                    raise RuntimeError(prep.get("error") or "Cookie 准备失败")
                root.after(0, lambda c=prep["cookie"]: sync_cookie_box(c))
                maybe_sync_cookie(prep["cookie"])
                for shop in shops:
                    if stop_flag.get("stop"):
                        break
                    name = str(shop.get("shop_name") or shop.get("tb_shop_id") or "")
                    try:
                        root.after(0, lambda n=name: log_line(f"重新扫描「{n}」…"))
                        root.after(0, lambda n=name: scan_now_var.set(f"正在扫：{n or '—'}"))
                        targets = resolve_rescan_targets(shop)
                        tb_id = str(targets.get("shop_id") or shop.get("tb_shop_id") or "")
                        seller = str(targets.get("user_id") or shop.get("seller_id") or "")
                        ids = list(targets["item_ids"] or [])
                        if fetch_limit > 0:
                            ids = ids[:fetch_limit]
                        root.after(
                            0,
                            lambda n=targets.get("notice") or "": log_line(n or f"共 {len(ids)} 个商品"),
                        )
                        items = fetch_details(
                            ids,
                            catalog_titles=targets.get("catalog_titles") or {},
                            delay=delay, progress_cb=on_progress, stop_flag=stop_flag,
                            wind_pause_after=wind_after, wind_pause_sec=wind_sec,
                            chrome_slider_wait=slider_wait,
                            delay_min=delay_min, delay_max=delay_max, backoff_factor=backoff,
                        )
                        ok_items = [x for x in items if x.get("ok")]
                        if not ok_items:
                            raise RuntimeError("没有抓到可用商品详情")
                        _, main_n, detail_n = _llm_from_bundle()
                        shop_body = {
                            "shop_name": name or str(targets.get("shop_name") or ""),
                            "shop_id": tb_id,
                            "user_id": seller,
                            "shop_link": shop.get("shop_link") or shop_seed_url(shop) or (
                                f"https://shop{tb_id}.taobao.com/" if tb_id else ""
                            ),
                        }
                        st = module_scan_save_md(
                            ok_items, shop_body, overwrite=True, do_ocr=True,
                            main_ocr_count=main_n, detail_ocr_count=detail_n,
                            progress_cb=lambda a, b, m: root.after(0, lambda: log_line(f"[{a}/{b}] {m}")),
                        )
                        last_path = str(st["path"])
                        src = ""
                        if st.get("path") and Path(st["path"]).is_file():
                            src = Path(st["path"]).read_text(encoding="utf-8")
                        upload_scan_results(
                            state["server"], state["token"], [], shop=shop_body,
                            source_md=src,
                        )
                        done_n += 1
                        root.after(0, lambda p=st["path"].name, a=st["added"]: log_line(
                            f"重新扫描完成 → {p}（写入 {a}）"
                        ))
                    except Exception as e:  # noqa: BLE001
                        fail_n += 1
                        err = str(e)
                        file_log(f"RESCAN_FAIL {name} {err}")
                        root.after(0, lambda n=name, m=err: log_line(f"重新扫描「{n}」失败: {m}"))
                root.after(0, lambda: scan_now_var.set("正在扫：—"))
                if done_n and fail_n == 0:
                    extra = f"\n{last_path}" if last_path else ""
                    root.after(0, lambda: messagebox.showinfo(
                        "重新扫描完成",
                        f"已覆盖生成本地店铺文档 {done_n} 家。{extra}\n可再点「分析店铺」。",
                    ))
                elif done_n:
                    root.after(0, lambda: messagebox.showwarning(
                        "重新扫描部分完成",
                        f"成功 {done_n} 家，失败 {fail_n} 家。详见日志。",
                    ))
                else:
                    raise RuntimeError("全部失败，详见日志")
            except Exception as e:  # noqa: BLE001
                err = str(e)
                file_log(f"RESCAN_FAIL {err}")
                root.after(0, lambda: scan_now_var.set("正在扫：—"))
                root.after(0, lambda: messagebox.showerror("重新扫描失败", err))

        t = threading.Thread(target=job, daemon=True)
        worker["thread"] = t
        t.start()

    def do_analyze_selected():
        if worker.get("thread") and worker["thread"].is_alive():
            messagebox.showwarning("进行中", "已有任务在跑")
            return
        try:
            shops = shops_for_action()
        except ValueError as e:
            messagebox.showerror("无法分析", str(e))
            return
        if len(shops) == 1:
            run_analyze_shop_job(shops[0])
            return

        def job():
            fail = 0
            for shop in shops:
                if stop_flag.get("stop"):
                    break
                name = str(shop.get("shop_name") or shop.get("tb_shop_id") or "")
                try:
                    root.after(0, lambda n=name: log_line(f"分析「{n}」…"))
                    analyze_shop_sync(shop, popup=False)
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    err = str(e)
                    file_log(f"ANALYZE_FAIL {name} {err}")
                    root.after(0, lambda n=name, m=err: log_line(f"分析「{n}」失败: {m}"))
            root.after(0, refresh_shops)
            if fail:
                root.after(0, lambda: messagebox.showwarning(
                    "分析结束", f"有 {fail} 家失败，详见日志",
                ))
            else:
                root.after(0, lambda: messagebox.showinfo("分析完成", f"已分析 {len(shops)} 家"))

        t = threading.Thread(target=job, daemon=True)
        worker["thread"] = t
        t.start()

    def do_scan_upload(items=None):
        """本机扫描后上传结果（云端只记库）。"""
        items = items if items is not None else state.get("items") or []
        if not items:
            messagebox.showwarning("无数据", "请先抓取")
            return
        if not state.get("token"):
            messagebox.showerror("未登录", "请先登录云端")
            return
        if worker.get("thread") and worker["thread"].is_alive():
            # 允许从抓取线程结束后调度进来；若仍在跑则排队到后台
            pass
        persist_cfg(token=state["token"])
        shop = {}
        m = state.get("meta") or {}
        if m.get("shop_id") and m.get("user_id"):
            shop = {
                "shop_id": m["shop_id"],
                "user_id": m["user_id"],
                "shop_name": m.get("shop_name") or "",
            }
        log_line("本机 OCR + 扫描中（云端只收结果）…")

        def _p(n, total, msg):
            root.after(0, lambda m=msg, a=n, b=total: log_line(f"[{a}/{b}] {m}" if b else m))

        def job_scan():
            try:
                res = run_local_scan_and_upload(
                    state["server"], state["token"], items, shop=shop or None,
                    progress_cb=_p,
                )
            except Exception as e:  # noqa: BLE001
                err = str(e)
                file_log(f"SCAN_UPLOAD_FAIL {err}")
                root.after(0, lambda: messagebox.showerror("扫描/上传失败", err))
                return
            if res.get("ok"):
                bad = res.get("bad_goods_sum", res.get("problem_count", 0))
                n = res.get("upserted") or res.get("goods_sum")
                root.after(0, lambda: log_line(
                    f"本机扫描完成并已入库：商品 {n} 个，有问题 {bad} 个。"
                    "请打开网页「我的店铺库」查看。"
                ))
                root.after(0, refresh_shops)
                root.after(0, lambda: messagebox.showinfo(
                    "完成",
                    f"本机已扫完并写入云端数据库。\n商品 {n} 个，有问题 {bad} 个。\n"
                    "打开网页 →「我的店铺库」查看。",
                ))
            else:
                root.after(0, lambda: messagebox.showerror(
                    "上传失败", res.get("error") or str(res),
                ))

        t = threading.Thread(target=job_scan, daemon=True)
        worker["thread"] = t
        t.start()

    def fetch_and_scan_one(
        work_url: str,
        *,
        popup: bool,
        fetch_limit: int,
        delay: float,
        delay_min: float,
        delay_max: float,
        backoff: float,
        wind_after: int,
        wind_sec: float,
        slider_wait: float,
    ) -> dict:
        """同步：解析种子 → 抓详情 → OCR md → 分析入库。须在工作线程。"""
        from link_harvest import shop_already_scanned
        from link_queue import find_by_item_id, mark_status, next_pending
        from shop_pipeline import module_scan_save_md

        work_url = (work_url or "").strip()
        if not work_url:
            raise ValueError("抓取 URL 为空")
        seed_id = ""
        targets: dict = {}
        while True:
            if stop_flag.get("stop"):
                return {"ok": False, "stopped": True}
            seed_ids = _extract_item_ids(work_url)
            seed_id = seed_ids[0] if seed_ids else ""
            qrow = find_by_item_id(seed_id) if seed_id else None
            targets = resolve_targets(url=work_url)
            shop_id = str(targets.get("shop_id") or "")
            shop_name = str(targets.get("shop_name") or "")
            is_queue = bool(qrow and str(qrow.get("status") or "") == "pending")
            if is_queue and (shop_id or shop_name):
                if shop_already_scanned(
                    shop_id, shop_name, state.get("shops_data") or [],
                ):
                    mark_status(
                        item_id=seed_id,
                        shop_id=shop_id,
                        status="skipped_dup",
                        note="开扫时发现店铺已扫过",
                    )
                    nxt = next_pending()
                    root.after(0, refresh_queue_ui)
                    if not nxt:
                        return {"ok": False, "need_harvest": True}
                    work_url = str(nxt.get("url") or "")
                    root.after(
                        0,
                        lambda u=work_url: (
                            ent_url.delete(0, tk.END),
                            ent_url.insert(0, u),
                        ),
                    )
                    root.after(
                        0,
                        lambda n=nxt: log_line(
                            f"店铺已扫过，换下一条 id={n.get('item_id')}"
                        ),
                    )
                    continue
            break

        ids = list(targets["item_ids"] or [])
        notice = targets.get("notice") or f"共 {len(ids)} 个商品"
        if fetch_limit > 0:
            ids = ids[:fetch_limit]
            notice = f"{notice} → 按条数限制抓前 {len(ids)} 个"
        root.after(0, lambda n=notice: log_line(n))
        items = fetch_details(
            ids,
            catalog_titles=targets.get("catalog_titles") or {},
            delay=delay,
            progress_cb=on_progress,
            stop_flag=stop_flag,
            wind_pause_after=wind_after,
            wind_pause_sec=wind_sec,
            chrome_slider_wait=slider_wait,
            delay_min=delay_min,
            delay_max=delay_max,
            backoff_factor=backoff,
        )
        if stop_flag.get("stop"):
            return {"ok": False, "stopped": True}
        state["items"] = items
        state["meta"] = {
            "shop_id": targets.get("shop_id") or "",
            "user_id": targets.get("user_id") or "",
            "shop_name": targets.get("shop_name") or "",
        }
        ok_n = sum(1 for x in items if x.get("ok"))
        fail_n = len(items) - ok_n
        for it in items[:20]:
            if not it.get("ok"):
                file_log(
                    f"FAIL id={it.get('id')} wind={it.get('wind_control')} "
                    f"err={(it.get('error') or '')[:300]}"
                )
        root.after(0, lambda: log_line(f"完成: 成功 {ok_n}，失败 {fail_n}"))
        file_log(f"DONE ok={ok_n} fail={fail_n} log={LOG_PATH}")
        try:
            shop_id_done = str(targets.get("shop_id") or "")
            if ok_n:
                mark_status(
                    item_id=seed_id,
                    shop_id=shop_id_done,
                    status="scanned",
                    note="已抓取扫描",
                )
            elif seed_id:
                mark_status(
                    item_id=seed_id,
                    status="failed",
                    note="详情均未抓到",
                )
            root.after(0, refresh_queue_ui)
        except Exception as e:  # noqa: BLE001
            file_log(f"QUEUE_MARK_FAIL {e}")
        _ensure_data()
        (CLIENT_DATA / "last_fetch.json").write_text(
            json.dumps({"meta": state["meta"], "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not ok_n:
            return {
                "ok": False,
                "fetch_failed": True,
                "shop_name": str(targets.get("shop_name") or ""),
            }
        shop_body = {
            "shop_name": state["meta"].get("shop_name") or "",
            "shop_id": state["meta"].get("shop_id") or "",
            "user_id": state["meta"].get("user_id") or "",
            "shop_link": state["meta"].get("shop_link") or "",
        }
        if not shop_body["shop_name"]:
            shop_body["shop_name"] = next(
                (str(x.get("title") or "") for x in items if x.get("title")),
                "",
            ) or "未命名店铺"
        _, main_n, detail_n = _llm_from_bundle()
        root.after(0, lambda: log_line("① 扫描：OCR 并写入本地 shops md…"))
        st = module_scan_save_md(
            [x for x in items if x.get("ok")],
            shop_body,
            overwrite=False,
            do_ocr=True,
            main_ocr_count=main_n,
            detail_ocr_count=detail_n,
            progress_cb=lambda a, b, m: root.after(
                0, lambda: log_line(f"[{a}/{b}] {m}")
            ),
        )
        root.after(0, lambda: log_line(f"① 扫描完成 → {st['path'].name}"))
        root.after(0, lambda: log_line("② 分析：对照词表/LLM 并上传…"))
        analyze_shop_sync(
            {
                **shop_body,
                "shop_name": st["shop_name"],
                "tb_shop_id": shop_body.get("shop_id") or "",
                "seller_id": shop_body.get("user_id") or "",
                "shop_link": st.get("shop_link") or shop_body.get("shop_link") or "",
            },
            popup=popup,
        )
        sid = str(shop_body.get("shop_id") or "")
        if sid:
            drop_pending_shop(tb_shop_id=sid)
        root.after(0, lambda n=st["shop_name"]: scan_now_var.set(f"正在扫：{n or '—'}"))
        return {
            "ok": True,
            "shop_name": st["shop_name"],
            "shop_id": shop_body.get("shop_id") or "",
        }

    def start_fetch_url(url: str, *, popup: bool) -> None:
        if worker.get("thread") and worker["thread"].is_alive():
            messagebox.showwarning("进行中", "已有任务在跑")
            return
        url = (url or "").strip()
        if not url:
            messagebox.showerror("缺少链接", "请粘贴要检查的店铺任意商品链接")
            return
        if not state.get("token"):
            messagebox.showerror("未登录", "请先登录云端")
            return
        stop_flag["stop"] = False
        cfg2 = persist_cfg(token=state.get("token") or "")
        delay = float(cfg2.get("item_delay_seconds") or DEFAULT_ITEM_DELAY)
        delay_min = float(cfg2.get("item_delay_min_seconds") or DEFAULT_DELAY_MIN)
        delay_max = float(cfg2.get("item_delay_max_seconds") or DEFAULT_DELAY_MAX)
        backoff = float(cfg2.get("wind_backoff_factor") or DEFAULT_BACKOFF)
        wind_after = int(cfg2.get("wind_control_pause_after") or DEFAULT_WIND_AFTER)
        wind_sec = float(cfg2.get("wind_control_pause_seconds") or DEFAULT_WIND_SEC)
        slider_wait = float(cfg2.get("chrome_slider_wait_seconds") or 180)
        login_wait = float(cfg2.get("chrome_login_wait_seconds") or 180)
        os.environ["ABSOLUTE_AUTO_SLIDER"] = str(cfg2.get("auto_slider") or "1")
        try:
            fetch_limit = int((ent_limit.get() or "0").strip() or "0")
        except ValueError:
            messagebox.showerror("抓取条数无效", "抓取条数必须是整数（0=全部）")
            return
        if fetch_limit < 0:
            messagebox.showerror("抓取条数无效", "抓取条数不能为负数")
            return
        pasted = txt_cookie.get("1.0", tk.END).strip()

        def job():
            try:
                def _p_cookie(msg: str) -> None:
                    root.after(0, lambda m=msg: log_line(m))

                prep = auto_prepare_cookie(
                    pasted=pasted,
                    login_url=login_url,
                    wait_seconds=login_wait,
                    progress_cb=_p_cookie,
                    stop_flag=stop_flag,
                )
                cookie = prep.get("cookie") or ""
                if not cookie:
                    raise RuntimeError(prep.get("error") or "自动准备 Cookie 失败")
                root.after(0, lambda c=cookie: sync_cookie_box(c))
                maybe_sync_cookie(cookie)
                _ensure_data()
                ck_now = load_fetch_cookie()
                if not ck_now:
                    raise RuntimeError(f"Cookie 未落到抓取路径: {COOKIE_PATH}")
                root.after(
                    0,
                    lambda n=len(ck_now), src=prep.get("source") or "":
                        log_line(f"Cookie 已就绪（{n} 字符，来源 {src}）→ 开始抓取"),
                )
                result = fetch_and_scan_one(
                    url,
                    popup=popup,
                    fetch_limit=fetch_limit,
                    delay=delay,
                    delay_min=delay_min,
                    delay_max=delay_max,
                    backoff=backoff,
                    wind_after=wind_after,
                    wind_sec=wind_sec,
                    slider_wait=slider_wait,
                )
                root.after(0, lambda: scan_now_var.set("正在扫：—"))
                if result.get("stopped"):
                    root.after(0, lambda: log_line("已停止"))
                    return
                if result.get("need_harvest"):
                    raise ValueError("待检查库里剩下的店铺都已扫过，请再点「获取店铺」")
                if result.get("fetch_failed"):
                    root.after(0, lambda: messagebox.showerror(
                        "全部失败",
                        f"详情均未抓到。\n详细日志: {LOG_PATH}\n结果: {CLIENT_DATA / 'last_fetch.json'}",
                    ))
            except Exception as e:  # noqa: BLE001
                err = str(e)
                file_log_exc("EXCEPTION", e)
                root.after(0, lambda: scan_now_var.set("正在扫：—"))
                root.after(0, lambda m=err: log_line(f"抓取失败: {m}"))
                root.after(0, lambda: messagebox.showerror("抓取失败", err))

        t = threading.Thread(target=job, daemon=True)
        worker["thread"] = t
        t.start()

    def run_fetch():
        url = "" if url_is_empty() else ent_url.get().strip()
        start_fetch_url(url, popup=True)

    def run_scan_pending(shop: dict) -> None:
        url = str(shop.get("shop_link") or "").strip()
        if not url:
            messagebox.showerror("无法扫描", "该店没有 shop_link")
            return
        name = str(shop.get("shop_name") or shop.get("tb_shop_id") or "")
        scan_now_var.set(f"正在扫：{name or '—'}")
        start_fetch_url(url, popup=True)

    def stop_fetch():
        stop_flag["stop"] = True
        log_line("已请求停止…")

    def run_harvest():
        if worker.get("thread") and worker["thread"].is_alive():
            messagebox.showwarning("进行中", "已有任务在跑")
            return
        if not state.get("token"):
            messagebox.showerror("未登录", "请先登录云端（采链需对照已扫店铺）")
            return
        try:
            count = harvest_count_now()
        except ValueError as e:
            messagebox.showerror("获取数量无效", str(e))
            return
        stop_flag["stop"] = False
        cfg2 = persist_cfg(token=state.get("token") or "")
        login_wait = float(cfg2.get("chrome_login_wait_seconds") or 180)
        pasted = txt_cookie.get("1.0", tk.END).strip()

        def job():
            try:
                def _p(msg: str) -> None:
                    root.after(0, lambda m=msg: log_line(m))

                prep = auto_prepare_cookie(
                    pasted=pasted,
                    login_url=login_url,
                    wait_seconds=login_wait,
                    progress_cb=_p,
                    stop_flag=stop_flag,
                )
                cookie = prep.get("cookie") or ""
                if not cookie:
                    raise RuntimeError(prep.get("error") or "自动准备 Cookie 失败")
                root.after(0, lambda c=cookie: sync_cookie_box(c))
                maybe_sync_cookie(cookie)
                res = harvest_to_cloud(cookie, count, _p)

                def _done() -> None:
                    refresh_pending_label()
                    log_line(
                        f"采链完成：新店 {res['added']} 家写入云端待检查库，"
                        f"已扫/已在队跳过 {res['skipped']}，失败 {res['failed']}。"
                        f"点「查看待检店铺」看列表，点「开始扫描」按列表逐家扫。"
                    )
                    for err in res.get("errors") or []:
                        log_line(f"  采链失败项: {err}")

                root.after(0, _done)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                file_log(f"HARVEST_FAIL {err}")
                root.after(0, lambda: messagebox.showerror("获取店铺失败", err))

        t = threading.Thread(target=job, daemon=True)
        worker["thread"] = t
        t.start()

    def run_auto_random():
        """开始扫描：按待检查库逐家扫。全自动在列表空后继续采新店。"""
        if worker.get("thread") and worker["thread"].is_alive():
            messagebox.showwarning("进行中", "已有任务在跑")
            return
        if not state.get("token"):
            messagebox.showerror("未登录", "请先登录云端")
            return
        from link_harvest import load_auto_random_config

        refill = auto_kind.get() == "full"
        try:
            auto_cfg = load_auto_random_config()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("扫描配置无效", str(e))
            return
        try:
            batch = harvest_count_now()
        except ValueError as e:
            messagebox.showerror("获取数量无效", str(e))
            return
        stop_flag["stop"] = False
        cfg2 = persist_cfg(token=state.get("token") or "")
        delay = float(cfg2.get("item_delay_seconds") or DEFAULT_ITEM_DELAY)
        delay_min = float(cfg2.get("item_delay_min_seconds") or DEFAULT_DELAY_MIN)
        delay_max = float(cfg2.get("item_delay_max_seconds") or DEFAULT_DELAY_MAX)
        backoff = float(cfg2.get("wind_backoff_factor") or DEFAULT_BACKOFF)
        wind_after = int(cfg2.get("wind_control_pause_after") or DEFAULT_WIND_AFTER)
        wind_sec = float(cfg2.get("wind_control_pause_seconds") or DEFAULT_WIND_SEC)
        slider_wait = float(cfg2.get("chrome_slider_wait_seconds") or 180)
        login_wait = float(cfg2.get("chrome_login_wait_seconds") or 180)
        os.environ["ABSOLUTE_AUTO_SLIDER"] = str(cfg2.get("auto_slider") or "1")
        try:
            fetch_limit = int((ent_limit.get() or "0").strip() or "0")
        except ValueError:
            messagebox.showerror("抓取条数无效", "抓取条数必须是整数（0=全部）")
            return
        if fetch_limit < 0:
            messagebox.showerror("抓取条数无效", "抓取条数不能为负数")
            return
        pasted = txt_cookie.get("1.0", tk.END).strip()
        max_shops = int(auto_cfg["max_shops"])
        pause_s = float(auto_cfg["pause_seconds"])
        popup = bool(auto_cfg["popup"])

        def job():
            done_n = 0
            fail_n = 0
            try:
                def _p(msg: str) -> None:
                    root.after(0, lambda m=msg: log_line(m))

                prep = auto_prepare_cookie(
                    pasted=pasted,
                    login_url=login_url,
                    wait_seconds=login_wait,
                    progress_cb=_p,
                    stop_flag=stop_flag,
                )
                cookie = prep.get("cookie") or ""
                if not cookie:
                    raise RuntimeError(prep.get("error") or "自动准备 Cookie 失败")
                root.after(0, lambda c=cookie: sync_cookie_box(c))
                maybe_sync_cookie(cookie)
                cap = f"最多 {max_shops} 家" if max_shops else "直到点停止"
                mode_s = "全自动（扫完列表后续采）" if refill else "半自动（只扫当前列表）"
                _p(f"开始扫描（{mode_s}，{cap}，每次采 {batch} 家，店间暂停 {pause_s}s）")
                skip_fail: set[int] = set()

                while True:
                    if stop_flag.get("stop"):
                        _p("已停止扫描")
                        break
                    if max_shops and done_n >= max_shops:
                        _p(f"已达上限 {max_shops} 家，结束")
                        break
                    d = api_request(
                        state["server"], "GET", "/scan-shops", token=state["token"],
                    )
                    raw = list(d.get("shops") or [])
                    shops = [s for s in raw if int(s.get("id") or 0) not in skip_fail]
                    if not shops:
                        if not refill:
                            if raw:
                                _p("待检查库剩余店铺本轮已失败，半自动结束")
                            elif done_n == 0 and fail_n == 0:
                                raise RuntimeError("待检查列表为空，请先点「获取店铺」")
                            else:
                                _p("列表已扫完（半自动不自动获取新店）")
                            break
                        if raw:
                            _p("待检查库剩余店铺本轮已失败，尝试再采新店…")
                        else:
                            _p("待检查库空，开始采链…")
                        try:
                            harvest_to_cloud(load_fetch_cookie() or cookie, batch, _p)
                        except Exception as e:  # noqa: BLE001
                            if raw:
                                _p(f"无法再采新店（{e}），结束")
                                break
                            raise
                        root.after(0, refresh_pending_label)
                        d = api_request(
                            state["server"], "GET", "/scan-shops", token=state["token"],
                        )
                        raw = list(d.get("shops") or [])
                        shops = [s for s in raw if int(s.get("id") or 0) not in skip_fail]
                        if not shops:
                            raise RuntimeError("采链后云端仍无新的待检查店铺")
                    shop = shops[0]
                    work_url = str(shop.get("shop_link") or "").strip()
                    if not work_url:
                        raise RuntimeError(
                            f"待检查记录没有 shop_link：id={shop.get('id')} "
                            f"tb_shop_id={shop.get('tb_shop_id')}"
                        )
                    name = str(shop.get("shop_name") or shop.get("tb_shop_id") or "")
                    root.after(0, lambda n=name: scan_now_var.set(f"正在扫：{n or '—'}"))
                    _p(
                        f"扫描第 {done_n + 1} 家"
                        f"{'' if not max_shops else f'/{max_shops}'}"
                        f"：「{name}」"
                    )
                    pk = int(shop.get("id") or 0)
                    try:
                        result = fetch_and_scan_one(
                            work_url,
                            popup=popup,
                            fetch_limit=fetch_limit,
                            delay=delay,
                            delay_min=delay_min,
                            delay_max=delay_max,
                            backoff=backoff,
                            wind_after=wind_after,
                            wind_sec=wind_sec,
                            slider_wait=slider_wait,
                        )
                    except Exception as e:  # noqa: BLE001
                        fail_n += 1
                        if pk:
                            skip_fail.add(pk)
                        file_log(f"AUTO_SHOP_FAIL {e}")
                        _p(f"这家失败，换下一家：{e}")
                        if stop_flag.get("stop"):
                            break
                        if pause_s > 0:
                            time.sleep(pause_s)
                        continue
                    if result.get("stopped"):
                        _p("已停止扫描")
                        break
                    if result.get("ok"):
                        done_n += 1
                        _p(f"已完成「{result.get('shop_name') or name}」（累计 {done_n} 家）")
                    elif result.get("fetch_failed"):
                        fail_n += 1
                        if pk:
                            skip_fail.add(pk)
                        _p("这家详情全失败，保留在待检查库，换下一家")
                    root.after(0, refresh_pending_label)
                    root.after(0, refresh_shops)
                    if stop_flag.get("stop"):
                        _p("已停止扫描")
                        break
                    if max_shops and done_n >= max_shops:
                        _p(f"已达上限 {max_shops} 家，结束")
                        break
                    if pause_s > 0:
                        _p(f"店间等待 {pause_s}s…")
                        deadline = time.time() + pause_s
                        while time.time() < deadline:
                            if stop_flag.get("stop"):
                                break
                            time.sleep(0.3)
                root.after(0, lambda: scan_now_var.set("正在扫：—"))
                _p(f"扫描结束：成功 {done_n} 家，失败 {fail_n} 家")
            except Exception as e:  # noqa: BLE001
                err = str(e)
                file_log(f"AUTO_RANDOM_FAIL {err}")
                root.after(0, lambda: scan_now_var.set("正在扫：—"))
                root.after(0, lambda: messagebox.showerror("扫描失败", err))
                root.after(0, lambda: log_line(
                    f"扫描中断：成功 {done_n} 家，失败 {fail_n} 家；{err}"
                ))

        t = threading.Thread(target=job, daemon=True)
        worker["thread"] = t
        t.start()

    btn_fetch = ttk.Button(btns, text="开始抓取", style="Primary.TButton", command=run_fetch)
    btn_fetch.pack(side=tk.LEFT)
    btn_auto_random = ttk.Button(btns, text="自动随机扫", style="Primary.TButton", command=run_auto_random)
    btn_stop = ttk.Button(btns, text="停止", command=stop_fetch)
    btn_stop.pack(side=tk.LEFT, padx=4)
    btn_harvest.configure(command=run_harvest)
    btn_semi_fetch.configure(command=run_harvest)
    btn_pending.configure(command=open_pending_window)
    btn_scan_list.configure(command=run_auto_random)
    apply_ui_mode()
    btn_rescan.configure(command=do_rescan_selected)
    btn_analyze.configure(command=do_analyze_selected)
    menu_settings.add_separator()
    menu_settings.add_command(label="打开抓取 Chrome", command=open_browser_ui)

    log_line(f"出错日志：{LOG_PATH}")
    log_line("单店模式：粘贴商品链接后点「开始抓取」。顶栏「模式」可选自动模式。")
    log_line("自动模式：「查看待检店铺」看列表并下载 Excel；「开始扫描」按列表逐家扫。全自动会在列表空后续采。")
    ck0 = load_fetch_cookie()
    log_line(
        f"Cookie：{'已缓存 ' + str(len(ck0)) + ' 字符' if ck0 else '无缓存（开始抓取时自动准备）'}"
    )
    file_log(f"USER_DIR {app_user_dir()} ini={CLIENT_INI} data={CLIENT_DATA}")
    try:
        abs_p, wrong_p = word_file_paths()
        def _word_n(p: Path) -> int:
            if not p.is_file():
                return 0
            return len([w for w in re.split(r"\s+", p.read_text(encoding="utf-8")) if w.strip()])

        n_abs, n_wrong = _word_n(abs_p), _word_n(wrong_p)
        file_log(f"WORDS abs={abs_p} n={n_abs} wrong={wrong_p} n={n_wrong}")
        if n_abs == 0 and n_wrong == 0:
            log_line(f"词表为空，扫描前请编辑：{abs_p}")
    except Exception as e:  # noqa: BLE001
        log_line(f"词表初始化失败: {e}")
        file_log_exc("WORD_INIT", e)
    try:
        file_log(f"CHROME {find_chrome_exe()}")
    except FileNotFoundError as e:
        log_line(str(e))
    def _ocr_probe() -> None:
        try:
            from scanner import probe_local_ocr

            ocr = probe_local_ocr()
            file_log(f"OCR_PROBE {ocr}")
            if ocr != "ok":
                root.after(0, lambda: log_line(f"OCR 不可用：{ocr}"))
        except Exception as e:  # noqa: BLE001
            file_log(f"OCR_PROBE_FAIL {e}")
            root.after(0, lambda: log_line(f"OCR 不可用：{e}"))

    threading.Thread(target=_ocr_probe, daemon=True).start()
    try:
        from shop_store import set_shops_dir

        actual = set_shops_dir(state.get("shops_dir"))
        state["shops_dir"] = str(actual)
        file_log(f"SHOPS_DIR {actual}")
        log_line(f"源文件目录：{actual}")
    except Exception as e:  # noqa: BLE001
        log_line(f"店铺目录初始化失败: {e}")

    def _refresh_client_token() -> str:
        """旧 token 失效时：用本地记住的账号静默重登，换不过期的 client 会话。"""
        cfg_now = load_client_config()
        user = (cfg_now.get("username") or state.get("username") or "").strip()
        pwd = cfg_now.get("password") or ""
        if not user or not pwd:
            return ""
        try:
            res = login_server(state["server"], user, pwd)
        except Exception as e:  # noqa: BLE001
            file_log(f"TOKEN_REFRESH_FAIL {e}")
            return ""
        tok = str(res.get("token") or "").strip()
        if not tok:
            return ""
        state["token"] = tok
        state["username"] = (res.get("user") or {}).get("username") or user
        state["balance"] = (res.get("user") or {}).get("balance")
        persist_cfg(token=tok, username=state["username"], password=pwd)
        try:
            set_login_ui(state["username"], state.get("balance"))
        except Exception:  # noqa: BLE001
            pass
        log_line(f"云端会话已自动续期: {state['username']}")
        return tok

    set_token_refresh(_refresh_client_token)
    try_auto_login()
    if not str(state.get("token") or "").strip():
        root.after(400, open_login_dialog)
    # 启动静默检查更新：有新版本先弹窗问是否升级，菜单栏入口同时保留
    def _silent_update():
        try:
            from app_update import fetch_app_update_info

            info = fetch_app_update_info(state["server"])
            if info.update_available:
                def _ui() -> None:
                    show_update_menubar(info.remote)
                    log_line(f"发现新版本 {info.remote}（当前 {info.current}）")
                    note = (info.note or "").strip()
                    msg = f"当前 {info.current} → 云端 {info.remote}\n{note}\n\n是否现在升级？" if note \
                        else f"当前 {info.current} → 云端 {info.remote}\n\n是否现在升级？"
                    if messagebox.askyesno("发现新版本", msg):
                        start_update_download(info)

                root.after(1200, _ui)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_silent_update, daemon=True).start()
    root.mainloop()



def run_cli(args: argparse.Namespace) -> int:
    cfg = load_client_config()
    server = args.server or cfg.get("server") or DEFAULT_SERVER
    token = cfg.get("token") or ""
    if args.username and args.password:
        res = login_server(server, args.username, args.password)
        token = res["token"]
        cfg["server"] = server
        cfg["username"] = args.username
        cfg["token"] = token
        save_client_config(cfg)
    if not token and args.upload:
        print("上传需要先 --username/--password 登录，或 GUI 登录后保存 token", file=sys.stderr)
        return 1

    if args.cookie_file:
        text = Path(args.cookie_file).read_text(encoding="utf-8")
        r = apply_cookie_text(text)
        if not r.get("valid"):
            print("Cookie 无效:", r.get("error"), file=sys.stderr)
            return 1
    elif args.chrome_cookie:
        r = read_chrome_cookie()
        if not r.get("valid"):
            print(r.get("error"), file=sys.stderr)
            return 1
    elif args.open_browser:
        open_local_browser(args.login_url or cfg.get("taobao_login_url") or DEFAULT_LOGIN_URL)
        print("已打开浏览器，登录后请用 --chrome-cookie 再跑", flush=True)
        return 0
    elif not load_local_cookie():
        print("请提供 --cookie-file / --chrome-cookie，或先 --open-browser", file=sys.stderr)
        return 1

    def prog(n, total, msg):
        print(f"[{n}/{total}] {msg}", flush=True)

    targets = resolve_targets(
        url=args.url or "",
        shop_id=args.shop_id or "",
        user_id=args.user_id or "",
    )
    print(targets.get("notice") or "", flush=True)
    items = fetch_details(
        targets["item_ids"],
        catalog_titles=targets.get("catalog_titles") or {},
        delay=args.delay,
        progress_cb=prog,
    )
    ok_n = sum(1 for x in items if x.get("ok"))
    print(f"完成: 成功 {ok_n} / {len(items)}", flush=True)
    _ensure_data()
    (CLIENT_DATA / "last_fetch.json").write_text(
        json.dumps({"meta": targets, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.upload:
        shop = None
        if targets.get("shop_id") and targets.get("user_id"):
            shop = {
                "shop_id": targets["shop_id"],
                "user_id": targets["user_id"],
                "shop_name": targets.get("shop_name") or "",
            }
        res = run_local_scan_and_upload(
            server, token, items, shop=shop, do_ocr=not args.no_ocr, progress_cb=prog,
        )
        print(json.dumps({k: res.get(k) for k in ("ok", "upserted", "bad_goods_sum", "problem_count")},
                         ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    return 0 if ok_n else 1


def main() -> int:
    p = argparse.ArgumentParser(description=APP_TITLE)
    p.add_argument("--cli", action="store_true", help="命令行模式（无 GUI）")
    p.add_argument("--url", default="", help="商品或店铺相关链接")
    p.add_argument("--shop-id", default="")
    p.add_argument("--user-id", default="")
    p.add_argument("--server", default="", help="API 根，如 https://host/absolute_term/api")
    p.add_argument("--username", default="", help="云端用户名")
    p.add_argument("--password", default="", help="云端密码")
    p.add_argument("--cookie-file", default="", help="Cookie/cURL 文本文件")
    p.add_argument("--chrome-cookie", action="store_true")
    p.add_argument("--open-browser", action="store_true", help="打开本机浏览器登录淘宝")
    p.add_argument("--login-url", default="", help="浏览器打开地址")
    p.add_argument("--upload", action="store_true")
    p.add_argument("--no-ocr", action="store_true")
    p.add_argument("--delay", type=float, default=DEFAULT_ITEM_DELAY)
    args = p.parse_args()
    if args.cli or args.url or args.shop_id or args.open_browser or args.username:
        return run_cli(args)
    run_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
