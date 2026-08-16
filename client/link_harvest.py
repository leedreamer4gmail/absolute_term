#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动采链：专用 Chrome 打开淘宝页抽取商品 id，已扫店铺跳过，其余写入待扫表。

扫描流水线不在这里；这里只负责往 link_queue 塞种子链接。
"""

from __future__ import annotations

import configparser
import os
import random
from pathlib import Path
from typing import Callable
from urllib.parse import quote

DEFAULT_SERVER = os.environ.get(
    "ABSOLUTE_API",
    "https://leedreamer.cn/absolute_term/api",
)


def _root_ini() -> configparser.ConfigParser:
    """先读安装包/仓库应用配置，再用云端 GET /ui 覆盖采链参数。"""
    from shop_store import app_config_ini_paths, client_ini_path

    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    found = False
    for ini in app_config_ini_paths():
        if ini.is_file():
            cfg.read(str(ini), encoding="utf-8")
            found = True
            break
    if not found:
        tried = " ; ".join(str(p) for p in app_config_ini_paths())
        raise FileNotFoundError(f"缺少配置文件: {tried}")
    _overlay_cloud_ui(cfg, client_ini_path())
    return cfg


def _client_server(client_ini: Path) -> str:
    server = DEFAULT_SERVER
    if client_ini.is_file():
        local = configparser.ConfigParser()
        local.optionxform = str
        try:
            local.read(str(client_ini), encoding="utf-8")
        except configparser.Error:
            local = None
        if local is not None and local.has_option("cloud", "server"):
            s = (local.get("cloud", "server") or "").strip()
            if s:
                server = s
    return server.rstrip("/")


def _overlay_cloud_ui(cfg: configparser.ConfigParser, client_ini: Path) -> None:
    """云端 [client] 采链/自动扫参数覆盖本地包内配置。失败不阻断，用包内值。"""
    import json
    import urllib.error
    import urllib.request

    keys = (
        "link_harvest_url",
        "link_harvest_search_url",
        "link_harvest_keyword",
        "link_harvest_count",
        "link_harvest_max_try",
        "link_harvest_wait_seconds",
        "auto_random_max_shops",
        "auto_random_pause_seconds",
        "auto_random_popup_problems",
    )
    url = _client_server(client_ini) + "/ui"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if not cfg.has_section("client"):
        cfg.add_section("client")
    for k in keys:
        val = data.get(k)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            cfg.set("client", k, text)


def _req(cfg: configparser.ConfigParser, section: str, key: str) -> str:
    if not cfg.has_option(section, key):
        raise RuntimeError(f"未配置 [{section}] {key}（config.ini）")
    val = (cfg.get(section, key) or "").strip()
    if not val:
        raise RuntimeError(f"[{section}] {key} 为空，请在 config.ini 或网页设置里填写")
    return val


def load_harvest_config() -> dict:
    """config.ini [client] 采链参数；缺项或非法直接报错。"""
    cfg = _root_ini()
    url = _req(cfg, "client", "link_harvest_url")
    search = _req(cfg, "client", "link_harvest_search_url")
    if "{q}" not in search:
        raise RuntimeError(
            "[client] link_harvest_search_url 必须含 {q} 占位符，"
            "例如 https://s.taobao.com/search?q={q}"
        )
    keyword = _req(cfg, "client", "link_harvest_keyword")
    try:
        count = int(_req(cfg, "client", "link_harvest_count"))
        max_try = int(_req(cfg, "client", "link_harvest_max_try"))
        wait_s = float(_req(cfg, "client", "link_harvest_wait_seconds"))
    except ValueError as e:
        raise RuntimeError(f"[client] 采链数字参数无法解析: {e}") from e
    if count < 1:
        raise RuntimeError("[client] link_harvest_count 必须 ≥ 1")
    if max_try < count:
        raise RuntimeError("[client] link_harvest_max_try 必须 ≥ link_harvest_count")
    if wait_s < 1:
        raise RuntimeError("[client] link_harvest_wait_seconds 必须 ≥ 1")
    return {
        "url": url,
        "search_url": search,
        "keyword": keyword,
        "count": count,
        "max_try": max_try,
        "wait_seconds": wait_s,
    }


def load_auto_random_config() -> dict:
    """自动随机扫：最多几家、店间暂停、是否每店弹问题窗。"""
    cfg = _root_ini()
    try:
        max_shops = int(_req(cfg, "client", "auto_random_max_shops"))
        pause = float(_req(cfg, "client", "auto_random_pause_seconds"))
    except ValueError as e:
        raise RuntimeError(f"[client] 自动随机扫数字参数无法解析: {e}") from e
    popup_raw = _req(cfg, "client", "auto_random_popup_problems")
    if max_shops < 0:
        raise RuntimeError("[client] auto_random_max_shops 必须 ≥ 0（0=直到点停止）")
    if pause < 0:
        raise RuntimeError("[client] auto_random_pause_seconds 不能为负")
    if popup_raw not in ("0", "1"):
        raise RuntimeError("[client] auto_random_popup_problems 只能是 0 或 1")
    return {
        "max_shops": max_shops,
        "pause_seconds": pause,
        "popup": popup_raw == "1",
    }


def shop_already_scanned(
    shop_id: str,
    shop_name: str,
    shops_data: list[dict] | None,
) -> bool:
    """云端已扫店铺 ∪ 本地 md。shop_id 与店名都空则无法判断，报错。"""
    sid = str(shop_id or "").strip()
    name = str(shop_name or "").strip()
    if not sid and not name:
        raise ValueError("无法判断是否已扫：shop_id 与 shop_name 都为空")
    for s in shops_data or []:
        if sid and (
            str(s.get("tb_shop_id") or "").strip() == sid
            or str(s.get("shop_id") or "").strip() == sid
        ):
            return True
        if name and str(s.get("shop_name") or "").strip() == name:
            return True
    if name:
        from shop_store import shop_md_path

        p = shop_md_path(name)
        if p.is_file() and p.stat().st_size > 0:
            return True
    return False


def harvest_into_queue(
    *,
    cookie: str,
    shops_data: list[dict] | None,
    progress_cb: Callable[[str], None] | None = None,
    stop_flag: dict | None = None,
    count: int | None = None,
    skip_shop=None,
    commit_pending=None,
) -> dict:
    """打开淘宝页抽商品链接，已扫店跳过，新店入 pending。扫描仍走原流水线。"""
    from chrome_fetch import ChromeFetcher
    from fetch_shop import resolve_shop_info
    from link_queue import enqueue, pending_count, pending_shop_ids

    def _p(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    def _stopped() -> bool:
        return bool(stop_flag and stop_flag.get("stop"))

    conf = load_harvest_config()
    target = int(count) if count is not None else int(conf["count"])
    if target < 1:
        raise ValueError("采链数量必须 ≥ 1")
    chrome = ChromeFetcher()
    try:
        _p(f"采链：打开 {conf['url']}")
        ids = chrome.collect_item_ids(
            conf["url"], wait_seconds=conf["wait_seconds"], progress_cb=progress_cb,
        )
        kw_parts = [x.strip() for x in str(conf["keyword"]).split(",") if x.strip()]
        if not kw_parts:
            raise RuntimeError("[client] link_harvest_keyword 拆开后为空")
        keyword = random.choice(kw_parts)
        q = quote(keyword, safe="")
        search = conf["search_url"].replace("{q}", q)
        _p(f"采链再搜：{keyword}")
        try:
            extra = chrome.collect_item_ids(
                search, wait_seconds=conf["wait_seconds"], progress_cb=progress_cb,
            )
            for iid in extra:
                if iid not in ids:
                    ids.append(iid)
        except Exception as e:  # noqa: BLE001
            _p(f"搜索页采链失败（继续用已抽到的 id）: {e}")
    finally:
        chrome.close()

    if not ids:
        raise RuntimeError(
            "采链未抽到任何商品 id。请确认专用 Chrome 已登录淘宝，"
            "或改 [client] link_harvest_url / link_harvest_keyword。"
        )

    random.shuffle(ids)
    added = 0
    skipped = 0
    failed = 0
    tried = 0
    have_shops = pending_shop_ids()
    errors: list[str] = []

    for iid in ids:
        if _stopped():
            raise RuntimeError("采链已停止")
        if added >= target:
            break
        if tried >= conf["max_try"]:
            break
        tried += 1
        _p(f"采链解析 {tried}/{conf['max_try']} id={iid}（已入队新店 {added}）")
        info = resolve_shop_info(iid, cookie=cookie)
        if "error" in info:
            failed += 1
            errors.append(f"{iid}: {info.get('error')}")
            enqueue(
                item_id=iid, status="failed",
                note=str(info.get("error") or "resolve_shop_info 失败")[:200],
            )
            continue
        shop_id = str(info.get("shop_id") or "").strip()
        shop_name = str(info.get("shop_name") or "").strip()
        seller_id = str(info.get("user_id") or "").strip()
        try:
            dup = shop_already_scanned(shop_id, shop_name, shops_data)
        except ValueError as e:
            failed += 1
            errors.append(f"{iid}: {e}")
            enqueue(item_id=iid, shop_id=shop_id, seller_id=seller_id,
                    shop_name=shop_name, status="failed", note=str(e))
            continue
        if not dup and callable(skip_shop):
            try:
                dup = bool(skip_shop(shop_id, shop_name))
            except Exception as e:  # noqa: BLE001
                failed += 1
                errors.append(f"{iid}: 云端去重失败 {e}")
                continue
        if dup:
            skipped += 1
            enqueue(
                item_id=iid, shop_id=shop_id, seller_id=seller_id,
                shop_name=shop_name, status="skipped_dup",
                note="店铺已扫过，换下一条",
            )
            _p(f"跳过已扫店铺「{shop_name or shop_id}」id={iid}")
            continue
        if commit_pending is None and shop_id and shop_id in have_shops:
            skipped += 1
            enqueue(
                item_id=iid, shop_id=shop_id, seller_id=seller_id,
                shop_name=shop_name, status="skipped_dup",
                note="待扫表已有同店种子",
            )
            continue
        row = {
            "item_id": iid,
            "shop_id": shop_id,
            "seller_id": seller_id,
            "shop_name": shop_name,
            "shop_link": f"https://item.taobao.com/item.htm?id={iid}",
            "tb_shop_id": shop_id,
            "url": f"https://item.taobao.com/item.htm?id={iid}",
        }
        if callable(commit_pending):
            committed = commit_pending(row)
            if committed is False:
                skipped += 1
                continue
        else:
            enqueue(
                item_id=iid, shop_id=shop_id, seller_id=seller_id,
                shop_name=shop_name, status="pending",
            )
        if shop_id:
            have_shops.add(shop_id)
        added += 1
        _p(f"入队待扫 id={iid} 店「{shop_name or shop_id}」")

    if added < 1:
        raise RuntimeError(
            f"采链结束但没有新店可入队（尝试 {tried} 个，已扫跳过 {skipped}，失败 {failed}）。"
            "请换关键词或确认已扫列表后重试。"
        )
    return {
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "tried": tried,
        "pending_total": pending_count(),
        "errors": errors[:8],
    }
