#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极限词本机抓取客户端：在用户出口抓淘宝详情，上传到服务器。

用法:
  python app.py
  python app.py --cli --url "https://item.taobao.com/item.htm?id=..." --server https://host/absolute/api
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # absolute_term/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cookie_util import pick_best_cookie, validate_cookie  # noqa: E402
from fetch_item import _session, fetch_item  # noqa: E402
from fetch_shop import (  # noqa: E402
    fetch_shop_catalog,
    parse_shop_key,
    resolve_shop_info,
)
from version import CLIENT_NAME, CLIENT_VERSION  # noqa: E402

CLIENT_DATA = HERE / "data"
COOKIE_PATH = CLIENT_DATA / "cookie.txt"
CONFIG_PATH = CLIENT_DATA / "client.json"
DEFAULT_SERVER = os.environ.get(
    "ABSOLUTE_API",
    "https://leedreamer.cn/absolute/api",
)
ITEM_DELAY = 2.0
WIND_PAUSE_AFTER = 3
WIND_PAUSE_SEC = 45.0


def _ensure_data() -> None:
    CLIENT_DATA.mkdir(parents=True, exist_ok=True)


def load_client_config() -> dict:
    _ensure_data()
    if not CONFIG_PATH.is_file():
        return {"server": DEFAULT_SERVER}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"server": DEFAULT_SERVER}


def save_client_config(cfg: dict) -> None:
    _ensure_data()
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def read_chrome_cookie() -> dict:
    """尝试从本机 Chrome 读取淘宝 Cookie。"""
    try:
        import browser_cookie3  # type: ignore
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "valid": False, "error": f"未安装 browser_cookie3: {e}"}
    try:
        jar = browser_cookie3.chrome(domain_name=".taobao.com")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "valid": False, "error": f"读取 Chrome Cookie 失败: {e}"}
    parts = [f"{c.name}={c.value}" for c in jar if c.value]
    if not parts:
        return {"ok": False, "valid": False, "error": "Chrome 中未找到淘宝 Cookie，请先浏览器登录淘宝"}
    cookie = "; ".join(parts)
    check = validate_cookie(cookie)
    if not check.get("valid"):
        return {**check, "cookie": cookie}
    save_local_cookie(cookie)
    return {**check, "ok": True, "cookie": cookie, "source": "chrome"}


def _extract_item_ids(url: str) -> list[str]:
    return re.findall(r"[?&]id=(\d{5,})", url or "")


def resolve_targets(url: str = "", shop_id: str = "", user_id: str = "",
                    cookie: str = "") -> dict:
    """解析要抓的商品 ID 列表。"""
    url = (url or "").strip()
    shop_id = (shop_id or "").strip()
    user_id = (user_id or "").strip()
    cookie = cookie or load_local_cookie()
    catalog_titles: dict[str, str] = {}
    meta = {"shop_id": shop_id, "user_id": user_id, "shop_name": "", "notice": ""}

    if shop_id and user_id:
        res = fetch_shop_catalog(shop_id, user_id, cookie=cookie, timeout=25)
        ids = [str(it["id"]) for it in res["items"]]
        for it in res["items"]:
            if it.get("title"):
                catalog_titles[str(it["id"])] = str(it["title"])
        meta["notice"] = res.get("notice") or f"获取到 {len(ids)} 个商品"
        return {"item_ids": ids, "catalog_titles": catalog_titles, **meta}

    if not url:
        raise ValueError("请填写商品/店铺链接，或 shop_id + user_id")

    ids = _extract_item_ids(url)
    if len(ids) == 1:
        info = resolve_shop_info(ids[0], cookie=cookie)
        if "error" not in info:
            meta["shop_id"] = info["shop_id"]
            meta["user_id"] = info["user_id"]
            meta["shop_name"] = info.get("shop_name") or ""
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

    # 店铺域名
    key = parse_shop_key(url)
    raise ValueError(
        f"识别到店铺域名 {key}，但本客户端请用「商品链接」或填写 shop_id+user_id"
    )


def fetch_details(item_ids: list[str], catalog_titles: dict[str, str] | None = None,
                  delay: float = ITEM_DELAY, progress_cb=None,
                  stop_flag: dict | None = None) -> list[dict]:
    """本机抓详情；失败如实标记，不降级冒充成功。"""
    catalog_titles = catalog_titles or {}
    os.environ["ABSOLUTE_COOKIE_FILE"] = str(COOKIE_PATH)
    sess = _session()
    results: list[dict] = []
    wind_streak = 0
    total = len(item_ids)

    for n, iid in enumerate(item_ids, 1):
        if stop_flag and stop_flag.get("stop"):
            break
        if progress_cb:
            progress_cb(n, total, f"抓取 {iid}")
        detail = fetch_item(iid, session=sess)
        title = detail.get("title") or catalog_titles.get(iid) or ""
        ok = bool(
            (title and title != iid)
            and (
                detail.get("detail_texts")
                or detail.get("detail_image_urls")
                or detail.get("main_image_urls")
            )
        )
        # 至少要有标题+（详情文本或任意图）；仅有列表标题不算成功
        has_detail = bool(detail.get("detail_texts") or detail.get("detail_image_urls"))
        wind = bool(detail.get("wind_control"))
        if wind:
            wind_streak += 1
            if wind_streak >= WIND_PAUSE_AFTER:
                if progress_cb:
                    progress_cb(n, total, f"风控暂停 {WIND_PAUSE_SEC:.0f}s…")
                time.sleep(WIND_PAUSE_SEC)
                detail = fetch_item(iid, session=sess)
                title = detail.get("title") or catalog_titles.get(iid) or title
                wind = bool(detail.get("wind_control"))
                has_detail = bool(detail.get("detail_texts") or detail.get("detail_image_urls"))
                wind_streak = 0 if not wind else wind_streak
        else:
            wind_streak = 0

        row = {
            "id": iid,
            "title": title or iid,
            "detail_texts": list(detail.get("detail_texts") or []),
            "main_image_urls": list(detail.get("main_image_urls") or []),
            "detail_image_urls": list(detail.get("detail_image_urls") or []),
            "error": detail.get("error") or "",
            "wind_control": wind,
            "ok": bool(has_detail and (title and title != iid)),
        }
        if not row["ok"] and title and title != iid and not has_detail:
            row["error"] = (row["error"] + " | 无详情图文").strip(" |")
        results.append(row)
        if progress_cb:
            flag = "OK" if row["ok"] else "FAIL"
            progress_cb(n, total, f"[{flag}] {n}/{total} {title[:40] or iid}")
        if n < total and delay > 0:
            time.sleep(delay)
    return results


def upload_goods(server: str, items: list[dict], shop: dict | None = None,
                 ocr: bool = True, replace: bool = True) -> dict:
    import urllib.error
    import urllib.request

    server = (server or "").rstrip("/")
    if not server.endswith("/api") and not server.endswith("/absolute/api"):
        # 允许填站点根；自动补 /absolute/api
        if server.endswith("/absolute"):
            server = server + "/api"
        else:
            server = server + "/absolute/api"
    url = server + "/goods/import"
    body = {
        "items": items,
        "ocr": bool(ocr),
        "replace": bool(replace),
        "client": CLIENT_NAME,
        "client_version": CLIENT_VERSION,
    }
    if shop:
        body["shop"] = shop
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"HTTP {e.code}: {raw[:300]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ---------- GUI ----------

def run_gui() -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
    except Exception as e:  # noqa: BLE001
        print(f"无 tkinter，请用 --cli 模式: {e}", file=sys.stderr)
        sys.exit(2)

    cfg = load_client_config()
    stop_flag = {"stop": False}
    worker: dict = {"thread": None}

    root = tk.Tk()
    root.title(f"极限词本机抓取 v{CLIENT_VERSION}")
    root.geometry("720x640")

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frm, text="服务器 API（上传地址）").grid(row=0, column=0, sticky=tk.W)
    ent_server = ttk.Entry(frm)
    ent_server.insert(0, cfg.get("server") or DEFAULT_SERVER)
    ent_server.grid(row=0, column=1, columnspan=3, sticky=tk.EW, pady=2)

    ttk.Label(frm, text="商品链接 / 店铺").grid(row=1, column=0, sticky=tk.W)
    ent_url = ttk.Entry(frm)
    ent_url.grid(row=1, column=1, columnspan=3, sticky=tk.EW, pady=2)

    ttk.Label(frm, text="shop_id（可选）").grid(row=2, column=0, sticky=tk.W)
    ent_shop = ttk.Entry(frm, width=18)
    ent_shop.grid(row=2, column=1, sticky=tk.W, pady=2)
    ttk.Label(frm, text="user_id").grid(row=2, column=2, sticky=tk.E)
    ent_user = ttk.Entry(frm, width=18)
    ent_user.grid(row=2, column=3, sticky=tk.W, pady=2)

    ttk.Label(frm, text="淘宝 Cookie / cURL（只存本机）").grid(row=3, column=0, sticky=tk.NW)
    txt_cookie = scrolledtext.ScrolledText(frm, height=6, wrap=tk.WORD)
    txt_cookie.grid(row=3, column=1, columnspan=3, sticky=tk.EW, pady=2)
    local_ck = load_local_cookie()
    if local_ck:
        txt_cookie.insert("1.0", local_ck)

    opt = ttk.Frame(frm)
    opt.grid(row=4, column=0, columnspan=4, sticky=tk.W, pady=4)
    var_ocr = tk.BooleanVar(value=True)
    var_upload = tk.BooleanVar(value=True)
    ttk.Checkbutton(opt, text="上传后服务器 OCR", variable=var_ocr).pack(side=tk.LEFT)
    ttk.Checkbutton(opt, text="抓完自动上传", variable=var_upload).pack(side=tk.LEFT, padx=8)
    ttk.Label(opt, text=f"间隔 {ITEM_DELAY:.0f}s/商品").pack(side=tk.LEFT)

    btns = ttk.Frame(frm)
    btns.grid(row=5, column=0, columnspan=4, sticky=tk.W, pady=4)

    log = scrolledtext.ScrolledText(frm, height=18, wrap=tk.WORD, state=tk.DISABLED)
    log.grid(row=6, column=0, columnspan=4, sticky=tk.NSEW, pady=6)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(6, weight=1)

    state: dict = {"items": [], "meta": {}}

    def log_line(msg: str) -> None:
        log.configure(state=tk.NORMAL)
        log.insert(tk.END, msg + "\n")
        log.see(tk.END)
        log.configure(state=tk.DISABLED)

    def on_progress(n, total, msg):
        root.after(0, lambda: log_line(f"[{n}/{total}] {msg}"))

    def save_cookie_ui():
        text = txt_cookie.get("1.0", tk.END)
        r = apply_cookie_text(text)
        if r.get("valid"):
            log_line(f"Cookie 已保存本机 score={r.get('score')} source={r.get('source')}")
            if r.get("warn"):
                log_line("警告: " + r["warn"])
        else:
            messagebox.showerror("Cookie 无效", r.get("error") or "无法识别")

    def chrome_cookie_ui():
        r = read_chrome_cookie()
        if r.get("valid") and r.get("cookie"):
            txt_cookie.delete("1.0", tk.END)
            txt_cookie.insert("1.0", r["cookie"])
            log_line("已从 Chrome 读取淘宝 Cookie")
        else:
            messagebox.showerror("读取失败", r.get("error") or "未知错误")

    def do_upload(items=None):
        items = items if items is not None else state.get("items") or []
        if not items:
            messagebox.showwarning("无数据", "请先抓取")
            return
        server = ent_server.get().strip()
        cfg2 = load_client_config()
        cfg2["server"] = server
        save_client_config(cfg2)
        shop = {}
        m = state.get("meta") or {}
        if m.get("shop_id") and m.get("user_id"):
            shop = {
                "shop_id": m["shop_id"],
                "user_id": m["user_id"],
                "shop_name": m.get("shop_name") or "",
            }
        log_line("正在上传…")
        res = upload_goods(server, items, shop=shop or None, ocr=var_ocr.get())
        if res.get("ok"):
            log_line(
                f"上传成功: 写入 {res.get('count')} 个"
                + (f"，OCR {res.get('ocr_done', 0)}" if res.get("ocr_done") is not None else "")
                + "。请回网页点「开始扫描」。"
            )
            messagebox.showinfo("上传成功", "请回网页点击「开始扫描」")
        else:
            messagebox.showerror("上传失败", res.get("error") or str(res))

    def run_fetch():
        if worker.get("thread") and worker["thread"].is_alive():
            messagebox.showwarning("进行中", "已有任务在跑")
            return
        stop_flag["stop"] = False
        ck_text = txt_cookie.get("1.0", tk.END).strip()
        if ck_text:
            r = apply_cookie_text(ck_text)
            if not r.get("valid"):
                messagebox.showerror("Cookie 无效", r.get("error") or "")
                return
        elif not load_local_cookie():
            messagebox.showerror("缺少 Cookie", "请粘贴 Cookie 或从 Chrome 读取")
            return

        url = ent_url.get().strip()
        shop_id = ent_shop.get().strip()
        user_id = ent_user.get().strip()

        def job():
            try:
                targets = resolve_targets(url=url, shop_id=shop_id, user_id=user_id)
                ids = targets["item_ids"]
                root.after(0, lambda: log_line(targets.get("notice") or f"共 {len(ids)} 个商品"))
                items = fetch_details(
                    ids,
                    catalog_titles=targets.get("catalog_titles") or {},
                    progress_cb=on_progress,
                    stop_flag=stop_flag,
                )
                state["items"] = items
                state["meta"] = {
                    "shop_id": targets.get("shop_id") or "",
                    "user_id": targets.get("user_id") or "",
                    "shop_name": targets.get("shop_name") or "",
                }
                ok_n = sum(1 for x in items if x.get("ok"))
                fail_n = len(items) - ok_n
                root.after(0, lambda: log_line(f"完成: 成功 {ok_n}，失败 {fail_n}"))
                # 本地缓存结果
                _ensure_data()
                (CLIENT_DATA / "last_fetch.json").write_text(
                    json.dumps({"meta": state["meta"], "items": items}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                if var_upload.get() and ok_n:
                    root.after(0, lambda: do_upload(items))
                elif fail_n and not ok_n:
                    root.after(0, lambda: messagebox.showerror(
                        "全部失败", "详情均未抓到，请检查 Cookie / 稍后重试，不要立刻连打",
                    ))
            except Exception as e:  # noqa: BLE001
                err = str(e)
                root.after(0, lambda: messagebox.showerror("抓取失败", err))

        t = threading.Thread(target=job, daemon=True)
        worker["thread"] = t
        t.start()

    def stop_fetch():
        stop_flag["stop"] = True
        log_line("已请求停止…")

    ttk.Button(btns, text="保存 Cookie", command=save_cookie_ui).pack(side=tk.LEFT)
    ttk.Button(btns, text="读 Chrome Cookie", command=chrome_cookie_ui).pack(side=tk.LEFT, padx=4)
    ttk.Button(btns, text="开始抓取", command=run_fetch).pack(side=tk.LEFT, padx=4)
    ttk.Button(btns, text="停止", command=stop_fetch).pack(side=tk.LEFT)
    ttk.Button(btns, text="上传到服务器", command=lambda: do_upload()).pack(side=tk.LEFT, padx=8)

    log_line(f"{CLIENT_NAME} v{CLIENT_VERSION} — Cookie 只存本机，不上传到服务器")
    log_line("流程：填 Cookie → 抓取 → 上传 → 回网页点「开始扫描」")
    root.mainloop()


def run_cli(args: argparse.Namespace) -> int:
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
    elif not load_local_cookie():
        print("请提供 --cookie-file 或 --chrome-cookie", file=sys.stderr)
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
        server = args.server or load_client_config().get("server") or DEFAULT_SERVER
        shop = None
        if targets.get("shop_id") and targets.get("user_id"):
            shop = {
                "shop_id": targets["shop_id"],
                "user_id": targets["user_id"],
                "shop_name": targets.get("shop_name") or "",
            }
        res = upload_goods(server, items, shop=shop, ocr=not args.no_ocr)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    return 0 if ok_n else 1


def main() -> int:
    p = argparse.ArgumentParser(description="极限词本机抓取客户端")
    p.add_argument("--cli", action="store_true", help="命令行模式（无 GUI）")
    p.add_argument("--url", default="", help="商品或店铺相关链接")
    p.add_argument("--shop-id", default="")
    p.add_argument("--user-id", default="")
    p.add_argument("--server", default="", help="API 根，如 https://host/absolute/api")
    p.add_argument("--cookie-file", default="", help="Cookie/cURL 文本文件")
    p.add_argument("--chrome-cookie", action="store_true")
    p.add_argument("--upload", action="store_true")
    p.add_argument("--no-ocr", action="store_true")
    p.add_argument("--delay", type=float, default=ITEM_DELAY)
    args = p.parse_args()
    if args.cli or args.url or args.shop_id:
        return run_cli(args)
    run_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
