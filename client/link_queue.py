#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待扫商品链接表：采链模块写入，扫描模块取出。落盘 client/data/link_queue.json。"""

from __future__ import annotations

import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PATH = HERE / "data" / "link_queue.json"

STATUSES = ("pending", "skipped_dup", "scanned", "failed")
STATUS_CN = {
    "pending": "待扫",
    "skipped_dup": "已扫过",
    "scanned": "已完成",
    "failed": "失败",
}
MAX_KEEP = 300


def queue_path() -> Path:
    from shop_store import link_queue_path as _user_queue

    return _user_queue()


def _empty() -> dict:
    return {"items": []}


def load_queue(path: Path | None = None) -> dict:
    p = path or queue_path()
    if not p.is_file():
        return _empty()
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return _empty()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"待扫链接表损坏（不是 JSON）: {p}\n{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"待扫链接表根节点必须是对象: {p}")
    items = data.get("items")
    if items is None:
        return _empty()
    if not isinstance(items, list):
        raise ValueError(f"待扫链接表 items 必须是数组: {p}")
    return {"items": items}


def save_queue(data: dict, path: Path | None = None) -> Path:
    p = path or queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    items = list(data.get("items") or [])
    if len(items) > MAX_KEEP:
        # 丢掉最旧的已结束项，保留全部 pending
        pending = [x for x in items if str(x.get("status") or "") == "pending"]
        rest = [x for x in items if str(x.get("status") or "") != "pending"]
        keep_rest = rest[-(MAX_KEEP - len(pending)) :] if len(pending) < MAX_KEEP else []
        items = pending + keep_rest
    payload = {"items": items}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def item_url(item_id: str) -> str:
    iid = str(item_id or "").strip()
    if not iid.isdigit():
        raise ValueError(f"商品 id 无效: {item_id!r}")
    return f"https://item.taobao.com/item.htm?id={iid}"


def find_by_item_id(item_id: str, path: Path | None = None) -> dict | None:
    iid = str(item_id or "").strip()
    if not iid:
        return None
    for it in load_queue(path)["items"]:
        if str(it.get("item_id") or "").strip() == iid:
            return dict(it)
    return None


def pending_shop_ids(path: Path | None = None) -> set[str]:
    out: set[str] = set()
    for it in load_queue(path)["items"]:
        if str(it.get("status") or "") != "pending":
            continue
        sid = str(it.get("shop_id") or "").strip()
        if sid:
            out.add(sid)
    return out


def next_pending(path: Path | None = None) -> dict | None:
    for it in load_queue(path)["items"]:
        if str(it.get("status") or "") == "pending":
            iid = str(it.get("item_id") or "").strip()
            if not iid.isdigit():
                raise ValueError(f"待扫表有无效商品 id: {it!r}")
            row = dict(it)
            row["url"] = str(row.get("url") or "").strip() or item_url(iid)
            return row
    return None


def pending_count(path: Path | None = None) -> int:
    return sum(1 for it in load_queue(path)["items"] if str(it.get("status") or "") == "pending")


def enqueue(
    *,
    item_id: str,
    shop_id: str = "",
    seller_id: str = "",
    shop_name: str = "",
    status: str = "pending",
    note: str = "",
    path: Path | None = None,
) -> dict:
    iid = str(item_id or "").strip()
    if not iid.isdigit():
        raise ValueError(f"商品 id 无效: {item_id!r}")
    if status not in STATUSES:
        raise ValueError(f"未知状态 {status!r}，允许: {STATUSES}")
    data = load_queue(path)
    for it in data["items"]:
        if str(it.get("item_id") or "").strip() == iid:
            it["shop_id"] = str(shop_id or it.get("shop_id") or "").strip()
            it["seller_id"] = str(seller_id or it.get("seller_id") or "").strip()
            it["shop_name"] = str(shop_name or it.get("shop_name") or "").strip()
            it["status"] = status
            it["note"] = str(note or it.get("note") or "")
            it["updated_at"] = _now()
            save_queue(data, path)
            return dict(it)
    row = {
        "item_id": iid,
        "url": item_url(iid),
        "shop_id": str(shop_id or "").strip(),
        "seller_id": str(seller_id or "").strip(),
        "shop_name": str(shop_name or "").strip(),
        "status": status,
        "note": str(note or ""),
        "added_at": _now(),
        "updated_at": _now(),
    }
    data["items"].append(row)
    save_queue(data, path)
    return dict(row)


def mark_status(
    *,
    item_id: str = "",
    shop_id: str = "",
    status: str,
    note: str = "",
    path: Path | None = None,
) -> int:
    """按商品 id 和/或店铺 id 改状态。返回改了几条。"""
    if status not in STATUSES:
        raise ValueError(f"未知状态 {status!r}，允许: {STATUSES}")
    iid = str(item_id or "").strip()
    sid = str(shop_id or "").strip()
    if not iid and not sid:
        raise ValueError("mark_status 需要 item_id 或 shop_id")
    data = load_queue(path)
    n = 0
    now = _now()
    for it in data["items"]:
        hit = False
        if iid and str(it.get("item_id") or "").strip() == iid:
            hit = True
        if sid and str(it.get("shop_id") or "").strip() == sid:
            hit = True
        if not hit:
            continue
        it["status"] = status
        if note:
            it["note"] = note
        it["updated_at"] = now
        n += 1
    if n:
        save_queue(data, path)
    return n


def list_items(path: Path | None = None) -> list[dict]:
    return [dict(x) for x in load_queue(path)["items"]]
