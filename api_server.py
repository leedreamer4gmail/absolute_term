#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极限词扫描 API：Unix socket，由 nginx 反代到 /absolute/api/。"""

from __future__ import annotations

import base64
import configparser
import csv
import io
import json
import os
import re
import socket
import sys
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.ini"
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
import threading
_TASK_LOCK = threading.Lock()


from llm_config import load_llm_config  # noqa: E402
from cookie_util import (  # noqa: E402
    _normalize_cookie,
    pick_best_cookie as _pick_best_cookie_raw,
    validate_cookie,
)


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
    return cfg


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


def load_cookie() -> str:
    """读取淘宝 Cookie；无效内容视为未配置。"""
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


def save_cookie(text: str, use_llm: bool = True) -> dict:
    """保存淘宝 Cookie。支持纯 Cookie / 单条或多条 curl，自动择优。"""
    picked = pick_best_cookie(text, use_llm=use_llm)
    if not picked.get("valid") or not picked.get("cookie"):
        return {**picked, **_cookie_saved_meta()}
    cookie = picked["cookie"]
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_PATH.write_text(cookie, encoding="utf-8")
    os.utime(COOKIE_PATH, None)
    status = cookie_status()
    status.update({
        "ok": True,
        "saved": True,
        "valid": True,
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
            "抓取完成后点上传；回本页点「开始扫描」",
        ],
        "note": "抓取走您本机网络，Cookie 默认不上传服务器。服务器「重新扫描」易被淘宝风控，不推荐。",
    }


def import_goods_from_client(payload: dict) -> dict:
    """接收本机客户端上传的商品详情，写入 goods.md，可选 OCR。"""
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


def load_shops() -> list[dict]:
    path = _shops_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    shops = data.get("shops") if isinstance(data, dict) else data
    if not isinstance(shops, list):
        return []
    out: list[dict] = []
    for s in shops:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("shop_id") or "").strip()
        uid = str(s.get("user_id") or "").strip()
        if not sid or not uid:
            continue
        item_ids = []
        raw_ids = s.get("item_ids") or []
        if isinstance(raw_ids, list):
            for x in raw_ids:
                xs = str(x).strip()
                if xs and xs not in item_ids:
                    item_ids.append(xs)
        item_titles: dict[str, str] = {}
        raw_titles = s.get("item_titles") or {}
        if isinstance(raw_titles, dict):
            for k, v in raw_titles.items():
                ks, vs = str(k).strip(), str(v).strip()
                if ks and vs:
                    item_titles[ks] = vs
        out.append({
            "shop_id": sid,
            "user_id": uid,
            "shop_name": str(s.get("shop_name") or "").strip() or sid,
            "sample_item_id": str(s.get("sample_item_id") or "").strip(),
            "item_ids": item_ids,
            "item_titles": item_titles,
            "item_count": len(item_ids),
            "updated_at": int(s.get("updated_at") or 0),
        })
    out.sort(key=lambda x: (-x["updated_at"], x["shop_name"]))
    return out


def save_shops(shops: list[dict]) -> Path:
    path = _shops_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "shops": [
            {
                "shop_id": s["shop_id"],
                "user_id": s["user_id"],
                "shop_name": s.get("shop_name") or s["shop_id"],
                "sample_item_id": s.get("sample_item_id") or "",
                "item_ids": list(s.get("item_ids") or []),
                "item_titles": {
                    str(k): str(v)
                    for k, v in (s.get("item_titles") or {}).items()
                    if str(k).strip() and str(v).strip()
                },
                "updated_at": int(s.get("updated_at") or 0),
            }
            for s in shops
        ]
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def upsert_shop(shop_id: str, user_id: str, shop_name: str = "",
                sample_item_id: str = "", item_ids: list[str] | None = None,
                item_titles: dict[str, str] | None = None) -> dict:
    shops = load_shops()
    shop_id = str(shop_id).strip()
    user_id = str(user_id).strip()
    now = int(time.time())
    found = None
    for s in shops:
        if s["shop_id"] == shop_id:
            found = s
            break
    if found is None:
        found = {"shop_id": shop_id, "user_id": user_id, "item_ids": [], "item_titles": {}}
        shops.append(found)
    found["user_id"] = user_id or found.get("user_id", "")
    if shop_name:
        found["shop_name"] = shop_name
    elif not found.get("shop_name"):
        found["shop_name"] = shop_id
    if sample_item_id:
        found["sample_item_id"] = sample_item_id
    if item_ids is not None:
        cleaned: list[str] = []
        for x in item_ids:
            xs = str(x).strip()
            if xs and xs not in cleaned:
                cleaned.append(xs)
        old = list(found.get("item_ids") or [])
        # 禁止用 CDN 首页那种个位数列表覆盖/冒充全店缓存
        if len(cleaned) >= 20 and len(cleaned) >= len(old):
            found["item_ids"] = cleaned
        elif len(cleaned) >= 20 and len(cleaned) > len(old):
            found["item_ids"] = cleaned
    if item_titles:
        merged = dict(found.get("item_titles") or {})
        for k, v in item_titles.items():
            ks, vs = str(k).strip(), str(v).strip()
            if ks and vs:
                merged[ks] = vs
        found["item_titles"] = merged
    found["updated_at"] = now
    save_shops(shops)
    return found


def get_shop(shop_id: str) -> dict | None:
    shop_id = str(shop_id or "").strip()
    if not shop_id:
        return None
    for s in load_shops():
        if s["shop_id"] == shop_id:
            return s
    return None


def delete_shop(shop_id: str) -> dict:
    shop_id = str(shop_id or "").strip()
    shops = load_shops()
    before = len(shops)
    shops = [s for s in shops if s["shop_id"] != shop_id]
    save_shops(shops)
    return {"ok": True, "deleted": before - len(shops), "count": len(shops)}


def shops_status() -> dict:
    shops = load_shops()
    return {
        "ok": True,
        "path": str(_shops_path().relative_to(ROOT)),
        "count": len(shops),
        "shops": shops,
    }


def add_shop_from_url(url: str) -> dict:
    """仅解析商品链接并写入店铺列表，不抓全店、不扫描。"""
    from fetch_shop import resolve_shop_info

    url = (url or "").strip()
    if not url:
        raise ValueError("请粘贴商品链接")
    item_ids = _extract_item_ids(url)
    if not item_ids:
        raise ValueError("链接里没有商品 ID，请粘贴商品页链接（含 id=）")
    cookie = load_cookie()
    if not cookie:
        raise ValueError("未配置 Cookie，无法解析店铺")
    info = resolve_shop_info(item_ids[0], cookie=cookie)
    if "error" in info:
        raise ValueError(info["error"])
    shop = upsert_shop(
        info["shop_id"], info["user_id"],
        shop_name=info.get("shop_name") or "",
        sample_item_id=item_ids[0],
    )
    return {"ok": True, "shop": shop, "all_item_count": info.get("all_item_count", 0)}


def create_task(url: str, ocr: bool, llm: bool, max_items: int, max_pages: int,
                force_rescan: bool = False, shop_id: str = "") -> str:
    """创建后台扫描任务，立即返回 task_id。"""
    import uuid
    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id, "url": url, "shop_id": shop_id, "status": "running",
        "current": 0, "total": 0, "current_title": "",
        "notice": "", "error": "", "results": [],
        "force_rescan": force_rescan,
    }
    with _TASK_LOCK:
        TASKS[task_id] = task

    def _run() -> None:
        try:
            result = scan_shop(
                url, ocr=ocr, llm=llm, max_items=max_items,
                max_pages=max_pages, force_rescan=force_rescan,
                shop_id=shop_id, progress_cb=_on_progress,
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
              force_rescan: bool = False, shop_id: str = "", progress_cb=None) -> dict:
    """扫描入口。

    - 不重新扫描：只从 file/goods.md 调用资料做检测
    - 重新扫描(force_rescan)：抓淘宝 → 写入 goods.md → 再检测
      可用已保存 shop_id（下拉选店），或粘贴商品/店铺链接（首次会写入店铺列表）
    """
    if not force_rescan:
        return scan_from_goods(url=url, llm=llm, max_items=max_items, progress_cb=progress_cb)

    import requests  # noqa: F401
    from fetch_item import _session, fetch_item
    from fetch_shop import fetch_shop_catalog, fetch_shop_items, parse_shop_key, resolve_shop_info

    cfg = _read_config()
    word_groups = load_word_groups()
    cookie = load_cookie()
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
        saved = get_shop(shop_id)
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
                saved["shop_id"], saved["user_id"],
                shop_name=shop_name,
                sample_item_id=item_ids[0] if item_ids else saved.get("sample_item_id", ""),
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
                    info["shop_id"], info["user_id"],
                    shop_name=shop_name,
                    sample_item_id=sample_id,
                )
                if progress_cb:
                    progress_cb(0, 0, f"已保存店铺「{shop_name}」")
                try:
                    saved0 = get_shop(info["shop_id"]) or {}
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
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
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

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        path = self._path()
        try:
            if path in ("/api/health", "/health"):
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if path in ("/api/scan", "/scan"):
                out_path = _read_config().get("scan", "output_file", fallback="output/违规清单.txt")
                report = (ROOT / out_path).read_text(encoding="utf-8")
                self._text(HTTPStatus.OK, report, "text/plain; charset=utf-8")
                return
            if path in ("/api/config", "/config"):
                report = CONFIG_PATH.read_text(encoding="utf-8")
                self._text(HTTPStatus.OK, report, "text/plain; charset=utf-8")
                return
            if path in ("/api/ui", "/ui"):
                cfg = _read_config()
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "houtai_url": cfg.get("ui", "houtai_url", fallback="/houtai/").strip(),
                    "home_url": cfg.get("ui", "home_url", fallback="https://www.imocfood.com").strip(),
                })
                return
            if path in ("/api/llm", "/llm"):
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
                self._json(HTTPStatus.OK, cookie_status())
                return
            # 扫描任务进度
            if path in ("/api/scan/status", "/scan/status"):
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
                self._json(HTTPStatus.OK, files_status())
                return
            if path in ("/api/files/limit", "/files/limit"):
                self._json(HTTPStatus.OK, read_word_file("limit"))
                return
            if path in ("/api/files/wrong", "/files/wrong"):
                self._json(HTTPStatus.OK, read_word_file("wrong"))
                return
            if path in ("/api/goods", "/goods"):
                self._json(HTTPStatus.OK, goods_status())
                return
            if path in ("/api/goods/migrate", "/goods/migrate"):
                self._json(HTTPStatus.OK, migrate_goods_split())
                return
            if path in ("/api/client/info", "/client/info"):
                self._json(HTTPStatus.OK, client_info())
                return
            if path in ("/api/shops", "/shops"):
                self._json(HTTPStatus.OK, shops_status())
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except FileNotFoundError as e:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

    def do_POST(self) -> None:
        path = self._path()
        try:
            # 触发扫描
            if path in ("/api/scan", "/scan"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                ocr = data.get("ocr", True)
                llm = data.get("llm", True)
                report = run_scan(ocr=ocr, llm=llm)
                self._text(HTTPStatus.OK, report, "text/plain; charset=utf-8")
                return

            # 店铺链接自动扫描（后台任务，立即返回 task_id）
            if path in ("/api/scan/shop", "/scan/shop"):
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
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, delete_shop(data.get("shop_id") or ""))
                return

            # 仅用商品链接解析店铺并加入列表（不整店扫描）
            if path in ("/api/shops/add", "/shops/add"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, add_shop_from_url(data.get("url") or ""))
                return

            # 保存在线编辑词表
            if path in ("/api/files/limit", "/files/limit"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, save_word_file("limit", data.get("content", "")))
                return
            if path in ("/api/files/wrong", "/files/wrong"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, save_word_file("wrong", data.get("content", "")))
                return

            # 保存淘宝 Cookie
            if path in ("/api/cookie", "/cookie"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                use_llm = bool(data.get("use_llm", True))
                result = save_cookie(data.get("cookie", ""), use_llm=use_llm)
                status = HTTPStatus.OK if result.get("valid") else HTTPStatus.BAD_REQUEST
                self._json(status, result)
                return

            # 本机客户端上传商品资料
            if path in ("/api/goods/import", "/goods/import"):
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                self._json(HTTPStatus.OK, import_goods_from_client(data))
                return

            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON 无效"})
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


def main() -> None:
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "images").mkdir(parents=True, exist_ok=True)
    SOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
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