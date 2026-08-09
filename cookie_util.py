#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""淘宝 Cookie 解析/择优（服务端与本机客户端共用）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

def _normalize_cookie(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    m = re.match(r"^cookie\s*=\s*(.+)$", text, re.I | re.S)
    if m:
        text = m.group(1).strip().strip('"').strip("'")
    return text.strip()


def _unescape_curl_cmd(s: str) -> str:
    """还原 Windows Copy as cURL (cmd) 的 ^ 转义。"""
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("^%^", "%").replace("^&", "&").replace("^|", "|").replace("^<", "<")
    s = s.replace("^>", ">").replace("^(", "(").replace("^)", ")")
    s = s.replace("^!", "!").replace("^^", "^").replace('^"', '"').replace("^'", "'")
    return s


def _cookie_kv(cookie: str) -> dict[str, str]:
    kv: dict[str, str] = {}
    for p in (cookie or "").split(";"):
        p = p.strip()
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        kv[k.strip().lower()] = v.strip()
    return kv


def _looks_like_cookie(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 20 or ";" not in t or t.count("=") < 3:
        return False
    if t.lower().startswith("curl ") or "http://" in t[:20] or "https://" in t[:30]:
        return False
    keys = set(_cookie_kv(t))
    useful = {"cookie2", "unb", "_m_h5_tk", "sgcookie", "cna", "cookie17", "t"}
    return len(keys & useful) >= 2


def extract_cookie_candidates(text: str) -> list[dict]:
    """从纯 Cookie / 单条或多条 curl 中提取候选 Cookie。

    支持 bash curl、Windows cmd curl（含 ^ 转义）、-b / --cookie / -H Cookie。
    """
    raw = _unescape_curl_cmd(text or "")
    if not raw.strip():
        return []

    found: list[dict] = []

    def _add(cookie: str, source: str, host: str = "") -> None:
        cookie = _normalize_cookie(cookie)
        cookie = cookie.strip().strip("'").strip('"').strip()
        # 去掉 curl 残留的行续接反斜杠
        cookie = re.sub(r"\\\s*$", "", cookie).strip()
        if not _looks_like_cookie(cookie):
            return
        # 去重：同文字只留一条（保留信息更全的 source）
        for item in found:
            if item["cookie"] == cookie:
                if len(source) > len(item.get("source") or ""):
                    item["source"] = source
                    if host:
                        item["host"] = host
                return
        found.append({"cookie": cookie, "source": source, "host": host or ""})

    # 1) curl 块：按 curl 关键字切分
    parts = re.split(r"(?i)(?=^\s*curl\s+)", raw, flags=re.M)
    curl_parts = [p for p in parts if re.match(r"(?i)^\s*curl\s+", p or "")]
    if not curl_parts and re.search(r"(?i)\bcurl\s+", raw):
        curl_parts = [raw]

    for block in curl_parts:
        host = ""
        um = re.search(r"curl\s+(?:-[^\s]+\s+)*['\"]?(https?://[^'\"\s]+)", block, re.I)
        if um:
            try:
                from urllib.parse import urlparse
                host = urlparse(um.group(1)).netloc.lower()
            except Exception:  # noqa: BLE001
                host = ""
        # -b / --cookie
        for m in re.finditer(
            r"""(?:-b|--cookie)\s+(?:\$')?(['"])(.*?)\1""",
            block, re.I | re.S,
        ):
            _add(m.group(2), f"curl -b ({host or 'unknown'})", host)
        # 无引号 -b cookie2=...（少见）
        for m in re.finditer(r"""(?:-b|--cookie)\s+([^\s-][^\\\n]*)""", block, re.I):
            frag = m.group(1).strip().strip("'\"")
            if "cookie2=" in frag or "unb=" in frag:
                _add(frag, f"curl -b ({host or 'unknown'})", host)
        # -H 'Cookie: ...' / -H "Cookie: ..."
        for m in re.finditer(
            r"""-H\s+(?:\$')?(['"])\s*Cookie\s*:\s*(.*?)\1""",
            block, re.I | re.S,
        ):
            _add(m.group(2), f"curl -H Cookie ({host or 'unknown'})", host)

    # 2) 独立 Cookie: 行
    for m in re.finditer(r"(?im)^\s*Cookie\s*:\s*(.+)$", raw):
        _add(m.group(1), "Header Cookie:")

    # 3) 整段就是 Cookie
    plain = _normalize_cookie(raw)
    if _looks_like_cookie(plain):
        _add(plain, "raw cookie")
    elif not found:
        # 去掉开头纯 URL 后再试
        plain2 = re.sub(r"(?is)^\s*https?://\S+\s*", "", raw).strip()
        if _looks_like_cookie(plain2):
            _add(plain2, "raw cookie")

    return found


def score_cookie_candidate(cookie: str, host: str = "", source: str = "") -> dict:
    """按规则给候选 Cookie 打分（不调用 LLM）。

    规则（分数越高越好）：
    1. 必备：cookie2、unb —— 缺则大幅扣分 / 判无效
    2. 加分：_m_h5_tk、sgcookie、cookie17、_tb_token_、cna
    3. uc1/uc3/uc4 长度≥20 加分；若存在但<20（如 cookie15/nk2/nk4）重罚
    4. 来源 host 含 taobao.com / h5api 加分；仅 tmall 详情且 uc 残缺则不加
    5. 关键字段越多越好
    """
    kv = _cookie_kv(cookie)
    reasons: list[str] = []
    score = 0
    truncated: list[str] = []

    def has(k: str) -> bool:
        return bool(kv.get(k))

    def vlen(k: str) -> int:
        return len(kv.get(k) or "")

    # 必备
    if has("cookie2") and vlen("cookie2") >= 16:
        score += 40
        reasons.append("+40 含完整 cookie2")
    else:
        score -= 50
        reasons.append("-50 缺少 cookie2（HttpOnly 登录态，document.cookie 读不到）")

    if has("unb"):
        score += 15
        reasons.append("+15 含 unb")
    else:
        score -= 20
        reasons.append("-20 缺少 unb")

    # 常用
    for k, pts, label in (
        ("_m_h5_tk", 12, "含 _m_h5_tk"),
        ("_m_h5_tk_enc", 6, "含 _m_h5_tk_enc"),
        ("sgcookie", 12, "含 sgcookie"),
        ("cookie17", 6, "含 cookie17"),
        ("cookie1", 4, "含 cookie1"),
        ("_tb_token_", 4, "含 _tb_token_"),
        ("cna", 3, "含 cna"),
        ("t", 2, "含 t"),
    ):
        if has(k) and (k != "sgcookie" or vlen(k) >= 40):
            score += pts
            reasons.append(f"+{pts} {label}")
        elif k == "sgcookie" and has(k) and vlen(k) < 40:
            truncated.append(f"sgcookie(仅{vlen(k)}字符)")
            score -= 15
            reasons.append(f"-15 sgcookie 过短({vlen(k)})")

    # uc 家族：完整 vs 残缺
    for k, good_pts in (("uc1", 20), ("uc3", 12), ("uc4", 12)):
        n = vlen(k)
        if n <= 0:
            reasons.append(f"0 无 {k}")
            continue
        if n < 20:
            truncated.append(f"{k}(仅{n}字符)")
            score -= 25
            reasons.append(f"-25 {k} 残缺(仅{n}字符，常见于天猫详情页拷贝)")
        else:
            score += good_pts
            reasons.append(f"+{good_pts} {k} 完整({n}字符)")

    host_l = (host or "").lower()
    if "h5api.m.taobao.com" in host_l or "h5api.m.tmall.com" in host_l:
        score += 10
        reasons.append("+10 来自 h5api（抓取同系接口）")
    elif "www.taobao.com" in host_l or host_l.endswith(".taobao.com"):
        score += 8
        reasons.append("+8 来自 taobao.com（通常 uc* 完整）")
    elif "detail.tmall.com" in host_l or "tmall.com" in host_l:
        if truncated:
            score -= 5
            reasons.append("-5 来自天猫详情且 uc* 残缺，优先改用淘宝域 Cookie")
        else:
            score += 5
            reasons.append("+5 来自 tmall.com 且字段完整")

    score += min(10, max(0, (len(kv) - 10) // 2))
    if len(kv) >= 25:
        reasons.append(f"+字段数加分 keys={len(kv)}")

    usable = score >= 40 and has("cookie2") and has("unb") and not (
        "uc1" in {t.split("(")[0] for t in truncated}
        and "uc3" in {t.split("(")[0] for t in truncated}
    )
    # 更严：有残缺 uc 仍可用（cookie2 在），但标记 warn；usable 只要有 cookie2+unb+score
    usable = score >= 30 and has("cookie2") and has("unb")

    warn = ""
    if truncated:
        warn = "疑似不完整：" + "、".join(truncated)

    return {
        "score": score,
        "usable": usable,
        "keys": len(kv),
        "host": host,
        "source": source,
        "reasons": reasons,
        "truncated_fields": truncated,
        "warn": warn,
        "has_h5_tk": "_m_h5_tk" in kv,
        "has_cookie2": has("cookie2"),
        "uc1_len": vlen("uc1"),
        "uc3_len": vlen("uc3"),
        "uc4_len": vlen("uc4"),
    }


def _llm_pick_cookie(summaries: list[dict],
                     chat_fn: Callable | None = None,
                     config_path: Path | str | None = None) -> dict | None:
    """分数接近时用 LLM 辅助选择。只传摘要，不传完整 Cookie。"""
    if chat_fn is None:
        try:
            from llm_config import call_chat as chat_fn  # type: ignore
        except Exception:  # noqa: BLE001
            return None
    lines = [
        "你是电商抓取配置助手。下面有多条淘宝/天猫 Cookie 候选摘要，请选最适合服务器端 mtop 抓取的一条。",
        "硬规则：必须有 cookie2+unb；uc1/uc3/uc4 长度≥20 优于残缺（cookie15/nk2/nk4）；",
        "优先 h5api / www.taobao.com；detail.tmall.com 且 uc 残缺的应淘汰。",
        "只输出 JSON：{\"index\": 候选编号, \"reason\": \"一句话\"}",
        "",
    ]
    for s in summaries:
        lines.append(
            f"#{s['index']} score={s['score']} host={s.get('host') or '-'} "
            f"keys={s['keys']} cookie2={s['has_cookie2']} h5={s['has_h5_tk']} "
            f"uc1_len={s['uc1_len']} uc3_len={s['uc3_len']} uc4_len={s['uc4_len']} "
            f"trunc={','.join(s.get('truncated_fields') or []) or '-'} "
            f"source={s.get('source')}"
        )
    kwargs = {"temperature": 0.1, "timeout": 60}
    if config_path is not None:
        kwargs["config_path"] = config_path
    try:
        out = chat_fn(
            [{"role": "user", "content": "\n".join(lines)}],
            **kwargs,
        )
    except Exception:  # noqa: BLE001
        return None
    text = (out or "").strip()
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "index" not in obj:
        return None
    return obj


def pick_best_cookie(text: str, use_llm: bool = True,
                     llm_config_path: Path | str | None = None) -> dict:
    """分析粘贴内容，选出最佳 Cookie。

    返回: ok/valid/cookie/score/reasons/candidates_summary/pick_method/warn/error
    """
    # 纯商品链接
    t = (text or "").strip()
    if re.fullmatch(r"https?://\S+", t) and "curl" not in t.lower():
        return {
            "ok": False, "saved": False, "valid": False,
            "error": "你粘贴的是商品链接。请粘贴 Cookie，或整段 Copy as cURL（可多条）",
        }

    cands = extract_cookie_candidates(text)
    if not cands:
        # 兼容旧逻辑：整段当 cookie
        one = _normalize_cookie(text)
        if _looks_like_cookie(one):
            cands = [{"cookie": one, "source": "raw cookie", "host": ""}]
        else:
            return {
                "ok": False, "saved": False, "valid": False,
                "error": "未识别到 Cookie。请粘贴 Cookie 串，或一条/多条 curl（含 -b 或 Cookie 头）",
            }

    scored: list[dict] = []
    for i, c in enumerate(cands):
        meta = score_cookie_candidate(c["cookie"], host=c.get("host") or "", source=c.get("source") or "")
        scored.append({**c, **meta, "index": i})

    scored.sort(key=lambda x: (x["usable"], x["score"], x["uc1_len"], x["keys"]), reverse=True)
    usable = [x for x in scored if x["usable"]]
    pool = usable or scored
    best = pool[0]
    pick_method = "rules"
    llm_note = ""

    # 分数接近时可选 LLM（只比较摘要）
    if use_llm and len(pool) >= 2:
        top, second = pool[0], pool[1]
        if abs(top["score"] - second["score"]) <= 12 and top["usable"] and second["usable"]:
            summaries = [
                {
                    "index": x["index"],
                    "score": x["score"],
                    "host": x.get("host"),
                    "keys": x["keys"],
                    "has_cookie2": x["has_cookie2"],
                    "has_h5_tk": x["has_h5_tk"],
                    "uc1_len": x["uc1_len"],
                    "uc3_len": x["uc3_len"],
                    "uc4_len": x["uc4_len"],
                    "truncated_fields": x.get("truncated_fields") or [],
                    "source": x.get("source"),
                }
                for x in pool[:5]
            ]
            picked = _llm_pick_cookie(summaries, config_path=llm_config_path)
            if picked is not None:
                try:
                    idx = int(picked.get("index"))
                except (TypeError, ValueError):
                    idx = -1
                for x in pool:
                    if x["index"] == idx:
                        best = x
                        pick_method = "rules+llm"
                        llm_note = str(picked.get("reason") or "")
                        break

    check = validate_cookie(best["cookie"])
    # 汇总摘要给前端（不含完整 cookie）
    summary = [
        {
            "index": x["index"],
            "score": x["score"],
            "usable": x["usable"],
            "host": x.get("host") or "",
            "source": x.get("source") or "",
            "keys": x["keys"],
            "uc1_len": x["uc1_len"],
            "uc3_len": x["uc3_len"],
            "uc4_len": x["uc4_len"],
            "truncated_fields": x.get("truncated_fields") or [],
            "selected": x["cookie"] == best["cookie"],
        }
        for x in scored
    ]

    if not check.get("valid"):
        return {
            **check,
            "pick_method": pick_method,
            "candidates": summary,
            "score": best["score"],
            "reasons": best.get("reasons") or [],
        }

    return {
        "ok": True,
        "saved": False,
        "valid": True,
        "cookie": best["cookie"],
        "keys": check.get("keys"),
        "useful": check.get("useful"),
        "has_h5_tk": check.get("has_h5_tk"),
        "warn": best.get("warn") or check.get("warn") or "",
        "truncated_fields": best.get("truncated_fields") or [],
        "score": best["score"],
        "reasons": best.get("reasons") or [],
        "pick_method": pick_method,
        "llm_note": llm_note,
        "source": best.get("source") or "",
        "host": best.get("host") or "",
        "candidates": summary,
        "candidates_count": len(scored),
    }


def validate_cookie(text: str) -> dict:
    """校验是否为可用的淘宝 Cookie 字符串（不是商品链接）。"""
    text = _normalize_cookie(text)
    if not text:
        return {"ok": False, "saved": False, "valid": False, "error": "Cookie 为空"}
    if re.fullmatch(r"https?://\S+", text):
        return {
            "ok": False, "saved": False, "valid": False,
            "error": "你粘贴的是商品链接，不是 Cookie。请粘贴 Cookie 或整段 curl",
        }
    pairs = [p.strip() for p in text.split(";") if p.strip() and "=" in p]
    if len(pairs) < 3:
        return {
            "ok": False, "saved": False, "valid": False,
            "error": "不像 Cookie（应类似 cookie2=...; _m_h5_tk=...; unb=...）",
        }
    kv = _cookie_kv(text)
    keys = set(kv)
    useful = {"cookie2", "_m_h5_tk", "_m_h5_tk_enc", "unb", "cookie17", "sgcookie", "cna", "_tb_token_", "t"}
    hit = sorted(keys & useful)
    if not hit:
        return {
            "ok": False, "saved": False, "valid": False,
            "error": "Cookie 缺少淘宝关键字段（如 cookie2 / _m_h5_tk / unb）",
        }
    truncated = []
    for k, min_len in (("uc1", 20), ("uc3", 20), ("uc4", 20), ("sgcookie", 40)):
        if k in kv and len(kv[k]) < min_len:
            truncated.append(f"{k}(仅{len(kv[k])}字符)")
    warn = ""
    if truncated:
        warn = (
            "Cookie 疑似复制不完整：" + "、".join(truncated)
            + "。若同时粘贴了多条 curl，请点保存让程序自动选淘宝域完整 Cookie"
        )
    return {
        "ok": True, "saved": True, "valid": True,
        "keys": len(pairs), "useful": hit,
        "has_h5_tk": "_m_h5_tk" in keys,
        "warn": warn,
        "truncated_fields": truncated,
    }
