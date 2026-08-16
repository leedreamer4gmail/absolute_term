#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端 UI 布局读写：主窗几何、Paned 柱子、文本框高度、子窗几何。

全部落在 client/config.ini [layout]，调节后防抖写回。
"""

from __future__ import annotations

import configparser
import threading
from pathlib import Path
from typing import Callable

# 默认布局（首次打开 / 缺项时）
# layout_version 抬升会重置主窗柱子
# v4：抬高 left_sash 默认与最小值，避免「地包天」（日志过大、词表被压没）
DEFAULTS: dict[str, str] = {
    "layout_version": "4",
    "main_geometry": "1100x720",
    "main_sash": "560",
    "left_sash": "480",
    "cookie_height": "3",
    "words_height": "8",
    "log_height": "8",
    "md_geometry": "900x600",
    "md_sash": "240",
    "problems_geometry": "820x520",
    "settings_geometry": "480x260",
    "login_geometry": "360x200",
}

# 主窗左右柱 / 左栏上下柱：不允许拖到看不见
MAIN_SASH_MIN = 360
MAIN_SASH_RIGHT_MIN = 280
# 上栏至少要放下：链接+条数+Cookie+词表可见区+按钮（不能再是 180）
LEFT_SASH_MIN = 360
LEFT_SASH_BOTTOM_MIN = 100
# 窗体尚未映射完成时 winfo_* 会很小，此时禁止 clamp/持久化，否则会把 480 夹成最小值并写坏 ini
LAYOUT_READY_W = MAIN_SASH_MIN + MAIN_SASH_RIGHT_MIN
LAYOUT_READY_H = LEFT_SASH_MIN + LEFT_SASH_BOTTOM_MIN


def clamp_main_sash(pos: int, total_width: int) -> int:
    """水平柱子夹紧，保证左栏链接/词表与右栏店铺都可见。"""
    w = int(total_width or 0)
    if w < LAYOUT_READY_W:
        raise ValueError(f"主窗宽度未就绪: {w}")
    lo = MAIN_SASH_MIN
    hi = max(lo, w - MAIN_SASH_RIGHT_MIN)
    return max(lo, min(int(pos), hi))


def clamp_left_sash(pos: int, total_height: int) -> int:
    """竖直柱子夹紧：上栏（操作+词表）优先，禁止日志地包天。"""
    h = int(total_height or 0)
    if h < LAYOUT_READY_H:
        raise ValueError(f"左栏高度未就绪: {h}")
    lo = LEFT_SASH_MIN
    hi = max(lo, h - LEFT_SASH_BOTTOM_MIN)
    return max(lo, min(int(pos), hi))


def default_left_sash(total_height: int) -> int:
    """上栏约占 62%，且不低于 LEFT_SASH_MIN。"""
    h = int(total_height or 0)
    if h < LAYOUT_READY_H:
        return int(DEFAULTS["left_sash"])
    want = int(h * 0.62)
    return clamp_left_sash(want, h)
_lock = threading.Lock()
_timers: dict[str, threading.Timer] = {}


def _read_ini(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if path.is_file():
        try:
            cp.read(path, encoding="utf-8")
        except configparser.Error as e:
            raise RuntimeError(f"config.ini 无效: {e}") from e
    return cp


def load_layout(ini_path: Path) -> dict[str, str]:
    """读 [layout]；缺项用 DEFAULTS。layout_version 过旧时重置主窗柱子默认。"""
    out = dict(DEFAULTS)
    cp = _read_ini(ini_path)
    if cp.has_section("layout"):
        for k in DEFAULTS:
            if cp.has_option("layout", k):
                v = (cp.get("layout", k) or "").strip()
                if v:
                    out[k] = v
    # 版本抬升：重置柱子/默认高度，清掉「左栏=0」等坏持久化
    if (out.get("layout_version") or "") != DEFAULTS["layout_version"]:
        for k in ("main_sash", "left_sash", "cookie_height", "words_height", "log_height", "main_geometry"):
            out[k] = DEFAULTS[k]
        out["layout_version"] = DEFAULTS["layout_version"]
        try:
            save_layout(ini_path, {k: out[k] for k in (
                "layout_version", "main_sash", "left_sash",
                "cookie_height", "words_height", "log_height", "main_geometry",
            )})
        except Exception:  # noqa: BLE001
            pass
    # 已持久化但被压没的柱子：读入时夹紧（用默认窗高估算，避免沿用 180 地包天）
    try:
        ms = int(out.get("main_sash") or DEFAULTS["main_sash"])
        if ms < MAIN_SASH_MIN:
            out["main_sash"] = DEFAULTS["main_sash"]
    except ValueError as e:
        raise RuntimeError(f"config.ini [layout] main_sash 无效: {e}") from e
    try:
        ls = int(out.get("left_sash") or DEFAULTS["left_sash"])
        if ls < LEFT_SASH_MIN:
            out["left_sash"] = DEFAULTS["left_sash"]
    except ValueError as e:
        raise RuntimeError(f"config.ini [layout] left_sash 无效: {e}") from e
    return out


def save_layout(ini_path: Path, updates: dict) -> None:
    """合并写入 [layout]。updates 的值转成 str。"""
    if not updates:
        return
    with _lock:
        cp = _read_ini(ini_path)
        if not cp.has_section("layout"):
            cp.add_section("layout")
        # 先铺默认，再盖旧值，再盖 updates，保证文件里键齐全带注释意义
        for k, v in DEFAULTS.items():
            if not cp.has_option("layout", k):
                cp.set("layout", k, v)
        for k, v in updates.items():
            if v is None:
                continue
            cp.set("layout", str(k), str(v).strip())
        ini_path.parent.mkdir(parents=True, exist_ok=True)
        with ini_path.open("w", encoding="utf-8") as f:
            cp.write(f)


def debounce_save(
    ini_path: Path,
    updates: dict,
    *,
    key: str = "default",
    delay: float = 0.35,
) -> None:
    """防抖写布局，避免拖拽时狂写磁盘。"""

    def _fire() -> None:
        with _lock:
            _timers.pop(key, None)
        save_layout(ini_path, updates)

    with _lock:
        old = _timers.pop(key, None)
        if old is not None:
            try:
                old.cancel()
            except Exception:  # noqa: BLE001
                pass
        t = threading.Timer(delay, _fire)
        t.daemon = True
        _timers[key] = t
        t.start()


def bind_geometry_persist(
    win,
    ini_path: Path,
    layout_key: str,
    *,
    get_extra: Callable[[], dict] | None = None,
) -> None:
    """窗口 Configure 时把 geometry 写入 layout_key（防抖）。"""

    def on_cfg(_event=None) -> None:
        try:
            geo = win.winfo_geometry()
        except Exception:  # noqa: BLE001
            return
        # 忽略最小化等无效
        if not geo or "x" not in geo:
            return
        upd = {layout_key: geo}
        if get_extra:
            try:
                upd.update(get_extra() or {})
            except Exception:  # noqa: BLE001
                pass
        debounce_save(ini_path, upd, key=f"geo:{layout_key}")

    win.bind("<Configure>", on_cfg, add="+")


def ellipsize(text: str, max_chars: int) -> str:
    """超长标题用 … 截断。"""
    s = (text or "").strip() or "—"
    if max_chars < 4:
        return s[:max_chars] if max_chars > 0 else s
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"


def split_md_products(text: str) -> list[dict]:
    """把店铺 md 切成商品块 [{title, body}]；body 含该商品全文。"""
    import re

    from shop_store import is_junk_product_title

    raw = text or ""
    if not raw.strip():
        return []
    parts = re.split(r"(?=<!--\s*item_id:\s*\S+\s*-->)", raw)
    blocks: list[dict] = []
    for part in parts:
        part = part.strip("\n")
        if not part.strip():
            continue
        # 跳过文件头（店铺名等无 item_id 的前言）里若只有前言
        m_id = re.match(r"<!--\s*item_id:\s*(\S+)\s*-->\s*", part)
        title = ""
        m_title = re.search(r"(?m)^#\s+(.+?)\s*$", part)
        if m_title:
            t = m_title.group(1).strip()
            if t not in ("主图内容", "详情内容"):
                title = t
        if not title and m_id:
            title = m_id.group(1).strip()
        if not title:
            # 前言块：若没有任何商品标题，不当商品
            if not m_id:
                continue
            title = "（无标题）"
        if is_junk_product_title(title):
            continue
        blocks.append({"title": title, "body": part.strip() + "\n"})
    if blocks:
        return blocks
    # 兼容无 item_id：按一级标题切（跳过 主图/详情）
    chunks = re.split(r"(?m)(?=^#\s+(?!主图内容|详情内容).+)", raw)
    for part in chunks:
        part = part.strip()
        if not part:
            continue
        m_title = re.match(r"#\s+(.+?)\s*$", part.split("\n", 1)[0])
        if not m_title:
            continue
        title = m_title.group(1).strip()
        if title in ("主图内容", "详情内容"):
            continue
        if is_junk_product_title(title):
            continue
        blocks.append({"title": title, "body": part + "\n"})
    return blocks
