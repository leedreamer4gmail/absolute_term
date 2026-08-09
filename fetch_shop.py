#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""店铺商品列表抓取：淘宝/天猫店铺链接、商品链接 → 商品列表。

支持:
  1. CDN 接口(不受服务器 IP 风控): 从商品ID获取全店商品列表
  2. HTML 搜索页(需本地IP): 翻页解析 shopXXX.taobao.com/search.htm
  3. 商品链接/ID解析: 自动提取店铺信息

用法:
    from fetch_shop import fetch_shop_items, resolve_shop_info
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# 淘宝店铺 CDN 数据接口(不受 IP 风控)
CDN_SHOP_API = "https://alisitecdn.m.taobao.com/minidata/shop/index/downgrade.htm"


def parse_shop_key(url: str) -> str:
    """从店铺链接中解析 shop key（数字 ID 或天猫域名）。"""
    url = url.strip()
    m = re.search(r"shop(\d+)\.taobao\.com", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]shop_id=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"//([\w-]+)\.tmall\.com", url)
    if m:
        return m.group(1)
    raise ValueError("无法识别的店铺链接（支持 shopXXX.taobao.com / XXX.tmall.com）")


def _clean_title(t: str) -> str:
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").strip()
    return t[:120]


def _parse_items(html: str) -> list[dict]:
    """多策略从店铺搜索页 HTML 提取商品列表。"""
    items: dict[str, str] = {}
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        m = re.search(r"item\.htm\?id=(\d+)", a["href"])
        if not m:
            continue
        iid = m.group(1)
        title = (a.get("title") or "").strip() or a.get_text(" ", strip=True)[:80]
        items.setdefault(iid, _clean_title(title))
    for tag in soup.find_all(attrs={"data-id": True}):
        iid = str(tag["data-id"]).strip()
        if iid.isdigit() and iid not in items:
            title = (tag.get("title") or tag.get("data-title") or "").strip()
            items[iid] = _clean_title(title)
    ids = re.findall(r'"(?:itemId|auctionId|item_id)"\s*:\s*"?(\d{5,})"?', html)
    titles = [_clean_title(t) for t in re.findall(r'"(?:rawTitle|title)"\s*:\s*"([^"]+)"', html)]
    for n, iid in enumerate(ids):
        if iid not in items:
            items[iid] = titles[n] if n < len(titles) else ""
    return [
        {"id": k, "title": v, "url": f"https://item.taobao.com/item.htm?id={k}"}
        for k, v in items.items()
    ]


def _extract_item_ids_from_cdn(data: Any) -> list[str]:
    """从 CDN 返回的数据中递归提取所有 itemId / auctionId（含标量）。"""
    ids: list[str] = []

    def _add(v: Any) -> None:
        try:
            ids.append(str(int(v)))
        except (ValueError, TypeError):
            pass

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("itemIds", "auctionIds") and isinstance(v, list):
                    for item in v:
                        _add(item)
                elif k in ("itemId", "auctionId") and not isinstance(v, (dict, list)):
                    _add(v)
                else:
                    _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(data)
    return sorted(set(ids), key=lambda x: int(x))


def _as_item_id(v: Any) -> str:
    if v is None or v == "":
        return ""
    try:
        return str(int(v))
    except (TypeError, ValueError):
        s = str(v).strip()
        m = re.search(r"(\d{6,})", s)
        return m.group(1) if m else ""


def _ids_from_shop_rows(rows: list) -> list[str]:
    return [it["id"] for it in _items_from_shop_rows(rows)]


def _items_from_shop_rows(rows: list) -> list[dict]:
    """从店铺列表行提取 id/title/pic（详情风控时标题可作降级扫描）。"""
    items: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        iid = ""
        for k in ("itemId", "item_id", "auctionId", "nid", "id"):
            iid = _as_item_id(row.get(k))
            if iid:
                break
        if not iid:
            for k in ("auctionUrl", "itemUrl", "url", "detailUrl"):
                iid = _as_item_id(row.get(k))
                if iid:
                    break
        if not iid or iid in seen:
            continue
        seen.add(iid)
        title = ""
        for k in ("title", "rawTitle", "itemTitle", "name", "auctionTitle"):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                title = _clean_title(v)
                break
        pic = ""
        for k in ("picUrl", "pic_url", "pic", "img", "image", "pictUrl"):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                pic = v.strip()
                if pic.startswith("//"):
                    pic = "https:" + pic
                break
        items.append({
            "id": iid,
            "title": title,
            "pic": pic,
            "url": f"https://item.taobao.com/item.htm?id={iid}",
        })
    return items


def _shop_session(cookie: str = ""):
    from fetch_item import _session

    s = _session()
    if cookie:
        s.headers["Cookie"] = cookie
        for part in cookie.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            s.cookies.set(k.strip(), v.strip(), domain=".taobao.com")
    return s


def fetch_shop_items_mtop(shop_id: str, user_id: str, cookie: str = "",
                          page_size: int = 30, max_pages: int = 50,
                          timeout: int = 20) -> dict:
    """分页拉全店商品（需 Cookie）。

    优先 mtop.taobao.shop.simple.item.fetch；失败/风控则改用
    mtop.taobao.wsearch.appsearch (shopitemsearch)。
    天猫店 CDN 首页只有少数推荐款，不能当全店。
    """
    from fetch_item import _mtop_call

    s = _shop_session(cookie)
    by_id: dict[str, dict] = {}
    total_cnt = 0
    source = ""
    partial_note = ""

    def _merge(page_items: list[dict]) -> None:
        for it in page_items:
            iid = it["id"]
            old = by_id.get(iid)
            if not old:
                by_id[iid] = it
                continue
            if not old.get("title") and it.get("title"):
                old["title"] = it["title"]
            if not old.get("pic") and it.get("pic"):
                old["pic"] = it["pic"]

    # --- 路径 1: simple.item.fetch ---
    page = 1
    while page <= max_pages:
        payload = {
            "sellerId": str(user_id),
            "shopId": str(shop_id),
            "page": page,
            "pageSize": int(page_size),
        }
        try:
            d = _mtop_call(
                s, "mtop.taobao.shop.simple.item.fetch", "1.0", payload, timeout=timeout,
            )
        except RuntimeError as e:
            if by_id:
                partial_note = f"；simple 第 {page} 页风控，已返回部分"
                source = "simple.item.fetch"
                break
            break  # 改走 wsearch
        ret = " ".join(str(x) for x in (d.get("ret") or []))
        if "SUCCESS" not in ret.upper():
            break
        data = d.get("data") or {}
        try:
            total_cnt = int(data.get("totalCnt") or total_cnt or 0)
        except (TypeError, ValueError):
            pass
        rows = data.get("data") or []
        page_items = _items_from_shop_rows(rows if isinstance(rows, list) else [])
        if not page_items:
            break
        source = "simple.item.fetch"
        _merge(page_items)
        if not bool(data.get("hasNext")):
            break
        page += 1
        time.sleep(0.7)

    # --- 路径 2: wsearch shopitemsearch（simple 被风控或为空时）---
    if not by_id or (total_cnt and len(by_id) < max(1, int(total_cnt * 0.8))):
        w_by_id: dict[str, dict] = {}
        w_total = 0
        page = 1
        w_ok = False
        while page <= max_pages:
            payload = {
                "m": "shopitemsearch",
                "shopId": str(shop_id),
                "sellerId": str(user_id),
                "page": page,
                "n": 40,
                "sort": "_coefp",
            }
            try:
                d = _mtop_call(
                    s, "mtop.taobao.wsearch.appsearch", "1.0", payload, timeout=timeout,
                )
            except RuntimeError:
                if w_by_id or by_id:
                    partial_note += f"；wsearch 第 {page} 页风控，已返回已拉取部分"
                    break
                break
            ret = " ".join(str(x) for x in (d.get("ret") or []))
            if "SUCCESS" not in ret.upper():
                break
            data = d.get("data") or {}
            try:
                w_total = int(data.get("totalResults") or w_total or 0)
            except (TypeError, ValueError):
                pass
            rows = data.get("result") or data.get("itemsArray") or []
            page_items = _items_from_shop_rows(rows if isinstance(rows, list) else [])
            if not page_items:
                # 有些页 ID 只在 JSON 字符串里
                blob = json.dumps(data, ensure_ascii=False)
                page_ids: list[str] = []
                for m in re.finditer(r'(?:item_id|itemId|nid)"\s*:\s*"?(\d{6,})', blob):
                    iid = m.group(1)
                    if iid not in page_ids:
                        page_ids.append(iid)
                page_items = [
                    {"id": iid, "title": "", "pic": "",
                     "url": f"https://item.taobao.com/item.htm?id={iid}"}
                    for iid in page_ids
                ]
            if not page_items:
                break
            w_ok = True
            for it in page_items:
                iid = it["id"]
                old = w_by_id.get(iid)
                if not old:
                    w_by_id[iid] = it
                else:
                    if not old.get("title") and it.get("title"):
                        old["title"] = it["title"]
                    if not old.get("pic") and it.get("pic"):
                        old["pic"] = it["pic"]
            # 页大小约 20；不足一页视为结束
            if len(page_items) < 10:
                break
            if w_total and len(w_by_id) >= w_total:
                break
            page += 1
            time.sleep(0.9)
        if w_ok and len(w_by_id) > len(by_id):
            by_id = w_by_id
            total_cnt = w_total or total_cnt
            source = "wsearch.shopitemsearch"
        elif w_ok and w_by_id:
            _merge(list(w_by_id.values()))

    if not by_id:
        raise ValueError("全店列表接口未返回商品（可能被风控，请稍后重试或更新 Cookie）")

    items = list(by_id.values())
    titled = sum(1 for it in items if it.get("title"))
    notice = (
        f"全店列表({source or 'mtop'})获取到 {len(items)} 个商品"
        + (f"（店铺约 {total_cnt}）" if total_cnt else "")
        + (f"，其中 {titled} 个带标题" if titled else "")
        + partial_note
    )
    return {"items": items, "notice": notice, "total": len(items), "declared_total": total_cnt}


def fetch_shop_catalog(shop_id: str, user_id: str, cookie: str = "",
                       timeout: int = 20, cached_item_ids: list[str] | None = None,
                       cached_item_titles: dict[str, str] | None = None) -> dict:
    """优先全店 mtop；失败时用上次缓存的商品 ID/标题；最后才 CDN 首页（可能只有几个推荐款）。"""
    mtop_err = ""
    if cookie:
        try:
            return fetch_shop_items_mtop(
                shop_id, user_id, cookie=cookie, timeout=timeout,
            )
        except (ValueError, RuntimeError) as e:
            mtop_err = str(e)

    cached = [str(x).strip() for x in (cached_item_ids or []) if str(x).strip()]
    titles = cached_item_titles or {}
    if cached:
        items = [
            {
                "id": iid,
                "title": str(titles.get(iid) or "").strip(),
                "url": f"https://item.taobao.com/item.htm?id={iid}",
            }
            for iid in cached
        ]
        titled = sum(1 for it in items if it.get("title"))
        notice = f"接口暂不可用，使用上次缓存的 {len(items)} 个商品 ID"
        if titled:
            notice += f"（{titled} 个带标题）"
        if mtop_err:
            notice = f"全店列表失败({mtop_err})；{notice}"
        return {"items": items, "notice": notice, "total": len(items), "from_cache_ids": True}

    try:
        res = fetch_shop_items_cdn(shop_id, user_id, timeout=timeout)
        warn = "CDN 仅店铺首页推荐款，通常远少于全店"
        if mtop_err:
            res["notice"] = f"全店列表失败({mtop_err})，已回退{warn}: {res['notice']}"
        else:
            res["notice"] = f"{warn}: {res['notice']}"
        return res
    except ValueError as e:
        if mtop_err:
            raise ValueError(f"全店列表失败({mtop_err}); CDN 也失败({e})") from e
        raise


def fetch_shop_items_cdn(shop_id: str, user_id: str, page_id: int | None = None,
                         timeout: int = 20) -> dict:
    """从淘宝 CDN 接口获取店铺商品列表(不受服务器 IP 风控影响)。

    返回: {"items": [...], "notice": "提示", "total": N}
    失败抛 ValueError。
    默认 pageId 对部分天猫店无效，会自动回退 pageId=0。
    """
    page_ids: list[int] = []
    if page_id is not None:
        page_ids.append(int(page_id))
    for pid in (326495798, 0):
        if pid not in page_ids:
            page_ids.append(pid)

    last_err = "CDN 接口返回空商品列表"
    for pid in page_ids:
        url = f"{CDN_SHOP_API}?pathInfo=shop/index2&userId={user_id}&shopId={shop_id}&pageId={pid}"
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            data = json.loads(r.text)
        except Exception as e:
            last_err = f"CDN 店铺数据获取失败(pageId={pid}): {e}"
            continue
        if isinstance(data, dict) and data.get("success") is False:
            last_err = f"CDN pageId={pid}: {data.get('message') or data.get('errorCode') or '失败'}"
            continue
        ids = _extract_item_ids_from_cdn(data)
        if not ids:
            last_err = f"CDN pageId={pid} 返回空商品列表"
            continue
        items = [
            {"id": iid, "title": "", "url": f"https://item.taobao.com/item.htm?id={iid}"}
            for iid in ids
        ]
        return {
            "items": items,
            "notice": f"CDN 接口获取到 {len(items)} 个商品(pageId={pid})",
            "total": len(items),
            "page_id": pid,
        }
    raise ValueError(last_err)


def _resolve_shop_from_html(item_id: str, cookie: str = "", timeout: int = 20) -> dict:
    """天猫等 getdetail 无 seller 时，从详情页 HTML 解析店铺信息。"""
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.taobao.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    pages = [
        f"https://detail.tmall.com/item.htm?id={item_id}",
        f"https://item.taobao.com/item.htm?id={item_id}",
    ]
    last = ""
    for url in pages:
        try:
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            html = r.text or ""
        except Exception as e:  # noqa: BLE001
            last = str(e)
            continue
        shop_ids = re.findall(r'["\']?shopId["\']?\s*[:=]\s*["\']?(\d{4,})', html)
        seller_ids = re.findall(r'["\']?sellerId["\']?\s*[:=]\s*["\']?(\d{4,})', html)
        names = re.findall(r'["\']shopName["\']\s*:\s*["\']([^"\']+)["\']', html)
        shop_id = shop_ids[0] if shop_ids else ""
        # sellerId 才是店主；userId 里常混进浏览者自己的 unb
        user_id = seller_ids[0] if seller_ids else ""
        shop_name = names[0] if names else ""
        if shop_id and user_id:
            return {
                "shop_id": shop_id,
                "user_id": user_id,
                "shop_name": shop_name,
                "all_item_count": 0,
                "source": "html",
            }
        last = f"{url} 未解析到 shopId/sellerId"
    return {"error": f"HTML 解析店铺失败: {last}"}


def resolve_shop_info(item_id: str, cookie: str = "", timeout: int = 20) -> dict:
    """从商品 ID 解析店铺信息(shopId, userId, shopName)。

    优先 mtop getdetail；天猫常无 seller 字段时回退详情页 HTML。
    失败返回 {"error": "原因"}。
    """
    from fetch_item import _mtop_call, _session, _token_expired

    s = _session()
    if cookie:
        s.headers["Cookie"] = cookie
        for part in cookie.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            s.cookies.set(k.strip(), v.strip(), domain=".taobao.com")
    try:
        d = _mtop_call(
            s,
            "mtop.taobao.detail.getdetail",
            "6.0",
            {"itemNumId": item_id, "exParams": "{}"},
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        html_info = _resolve_shop_from_html(item_id, cookie=cookie, timeout=timeout)
        if "error" not in html_info:
            return html_info
        return {"error": f"getdetail 请求失败: {e}; HTML: {html_info.get('error')}"}

    ret = " ".join(str(x) for x in (d.get("ret") or []))
    if _token_expired(ret):
        html_info = _resolve_shop_from_html(item_id, cookie=cookie, timeout=timeout)
        if "error" not in html_info:
            return html_info
        return {"error": f"getdetail 令牌过期: {ret}"}

    seller = (d.get("data") or {}).get("seller") or {}
    shop_id = str(seller.get("shopId") or "")
    user_id = str(seller.get("userId") or seller.get("sellerId") or "")
    shop_name = str(seller.get("shopName") or "")
    all_count = int(seller.get("allItemCount") or 0)
    if shop_id and user_id:
        return {
            "shop_id": shop_id,
            "user_id": user_id,
            "shop_name": shop_name,
            "all_item_count": all_count,
            "source": "mtop",
        }
    # 天猫：data.seller 常为 null，走 HTML
    html_info = _resolve_shop_from_html(item_id, cookie=cookie, timeout=timeout)
    if "error" not in html_info:
        return html_info
    return {"error": html_info.get("error") or f"getdetail 未返回店铺信息: {str(d)[:300]}"}


def fetch_shop_items(shop_key: str, cookie: str = "", max_pages: int = 5,
                     timeout: int = 20) -> dict:
    """抓店铺商品列表（HTML 搜索页解析）。

    返回: {"items": [...], "notice": "提示", "total": N}
    失败抛 ValueError(带原因)。
    提示: CDN 方式请先通过 resolve_shop_info() 获取 shopId+userId 后调用 fetch_shop_items_cdn()。
    """
    # 仅 HTML 搜索页解析(CDN 需要 shopId+userId 两个参数，无法从 shop_key 单独推断)
    if shop_key.isdigit():
        base = f"https://shop{shop_key}.taobao.com/search.htm"
    else:
        base = f"https://{shop_key}.tmall.com/search.htm"
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": "https://www.taobao.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if cookie:
        s.headers["Cookie"] = cookie

    all_items: dict[str, str] = {}
    notice = ""
    for page in range(1, max_pages + 1):
        try:
            resp = s.get(base, params={"search": "y", "pageNum": page}, timeout=timeout)
        except requests.RequestException as e:
            notice = f"第{page}页请求失败: {e}"
            break
        html = resp.text
        head = html[:2000]
        if "cloud_ip_bl" in resp.url or "deny_pc" in (resp.url + head):
            notice = ("淘宝风控拦截(服务器 IP 被列入黑名单)，店铺商品列表抓取失败。"
                      "请改用商品链接逐个扫描: 把商品链接粘贴到输入框, 多个用逗号分隔")
            break
        if "slide" in head.lower() or ("验证" in head and "item.htm" not in html):
            notice = "触发淘宝安全验证，请把浏览器最新 Cookie 粘贴到页面后再试"
            break
        if ("登录" in head and "item.htm" not in html) or "login.taobao.com" in resp.url:
            notice = "被跳转到登录页，请粘贴有效 Cookie"
            break
        page_items = _parse_items(html)
        if not page_items:
            break
        before = len(all_items)
        for it in page_items:
            all_items.setdefault(it["id"], it["title"])
        if len(all_items) == before:
            break
        time.sleep(0.4)

    if not all_items:
        raise ValueError(notice or "未解析到商品（店铺可能为空、页面结构变化或需要 Cookie）")
    items = [
        {"id": k, "title": v, "url": f"https://item.taobao.com/item.htm?id={k}"}
        for k, v in all_items.items()
    ]
    return {"items": items, "notice": notice, "total": len(items)}


if __name__ == "__main__":
    import sys

    # 测试: python3 fetch_shop.py <商品ID>  # 解析店铺信息并获取全部商品
    # 或: python3 fetch_shop.py <shop_key>  # 传统方式
    arg = sys.argv[1] if len(sys.argv) > 1 else "1055688226484"
    if re.fullmatch(r"\d{5,}", arg):
        # 尝试解析店铺信息
        from fetch_item import load_cookie
        info = resolve_shop_info(arg, load_cookie())
        if "error" in info:
            print(f"店铺解析失败: {info['error']}")
        else:
            print(f"店铺: {info['shop_name']} (shopId={info['shop_id']}, userId={info['user_id']}, 共{info['all_item_count']}商品)")
            r = fetch_shop_items_cdn(info['shop_id'], info['user_id'])
            print(f"CDN 获取到 {r['total']} 个商品:")
            for it in r["items"][:10]:
                print(f"  {it['id']}")
            if r['total'] > 10:
                print(f"  ... 共 {r['total']} 个")
    else:
        key = parse_shop_key(arg)
        r = fetch_shop_items(key)
        print(f"共 {r['total']} 个商品: {r['notice']}")
        for it in r["items"][:20]:
            print(it["id"], it["title"][:40])