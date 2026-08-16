#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机完成 OCR + 词表/LLM 扫描；云端只收最终结果落库。

LLM 的 api_url/model/api_key 只从云端 /client/bundle 拉取到内存，禁止写入客户端磁盘。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = HERE / "data"
# 历史误写文件：启动扫描时一律删除，避免密钥残留
_LEGACY_LLM_FILES = (DATA / "llm.ini", DATA / "scan_bundle.json")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scanner import (  # noqa: E402
    build_problem_md,
    filter_hits_by_judge,
    llm_judge,
    ocr_image_url_local,
    probe_local_ocr,
    scan_texts,
)


def _parse_words(text: str) -> list[str]:
    import re

    words: list[str] = []
    for piece in re.split(r"\s+", (text or "").strip()):
        piece = piece.strip()
        if piece and piece not in words:
            words.append(piece)
    return words


def purge_local_llm_secrets() -> None:
    """删除客户端曾误存的 LLM 密钥文件。"""
    from shop_store import writable_data_dir

    victims = list(_LEGACY_LLM_FILES)
    data = writable_data_dir()
    victims.extend((data / "llm.ini", data / "scan_bundle.json"))
    for p in victims:
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


def load_local_word_groups() -> list[tuple[str, list[str]]]:
    """本机词表：安装版读用户目录副本，源码读仓库 file/。"""
    from shop_store import word_file_paths

    groups: list[tuple[str, list[str]]] = []
    labels = ("极限词", "错误描述")
    paths = word_file_paths()
    for label, path in zip(labels, paths):
        if path.is_file():
            groups.append((label, _parse_words(path.read_text(encoding="utf-8"))))
        else:
            groups.append((label, []))
    return groups


def parse_bundle(bundle: dict) -> tuple[list[tuple[str, list[str]]], dict | None]:
    """解析云端 bundle：只取 LLM 到内存；词表一律用本地 file/，不采用云端词。"""
    purge_local_llm_secrets()
    groups = load_local_word_groups()

    llm = bundle.get("llm") or {}
    api_url = str(llm.get("api_url") or "").strip()
    model = str(llm.get("model") or "").strip()
    api_key = str(llm.get("api_key") or "").strip()
    judge_prompt = str(llm.get("judge_prompt") or "").strip()
    llm_conf = None
    if api_url and model and api_key:
        llm_conf = {
            "api_url": api_url,
            "model": model,
            "api_key": api_key,
        }
        if judge_prompt:
            llm_conf["judge_prompt"] = judge_prompt
    return groups, llm_conf


# 兼容旧名
def apply_bundle(bundle: dict) -> list[tuple[str, list[str]]]:
    groups, _ = parse_bundle(bundle)
    return groups


def _ocr_item(item: dict, main_n: int, detail_n: int, progress_cb: Callable[[str], None] | None) -> dict:
    import os
    import time

    def _lg(msg: str) -> None:
        path = (os.environ.get("ABSOLUTE_CLIENT_LOG") or "").strip()
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except OSError:
            pass

    iid = str(item.get("id") or "")
    main_urls = [str(u) for u in (item.get("main_image_urls") or [])[:main_n] if str(u).strip()]
    detail_urls = [str(u) for u in (item.get("detail_image_urls") or [])[:detail_n] if str(u).strip()]
    if not main_urls and not detail_urls:
        _lg(f"OCR_SKIP id={iid} 无主图/详情图 URL（抓取空，不是 OCR 没装）")
    main_ocr = [str(x) for x in (item.get("main_ocr") or []) if str(x).strip()]
    detail_ocr = [str(x) for x in (item.get("detail_ocr") or []) if str(x).strip()]
    main_err: list[str] = []
    detail_err: list[str] = []
    if not main_ocr:
        for u in main_urls:
            try:
                lines = ocr_image_url_local(str(u))
            except Exception as e:  # noqa: BLE001
                main_err.append(str(e))
                _lg(f"OCR_FAIL_MAIN id={iid} err={e}")
                continue
            if lines:
                main_ocr.append(" ".join(lines))
                if progress_cb:
                    progress_cb(f"OCR 主图 {item.get('id')}: {len(lines)} 行")
    if not detail_ocr:
        for u in detail_urls:
            try:
                lines = ocr_image_url_local(str(u))
            except Exception as e:  # noqa: BLE001
                detail_err.append(str(e))
                _lg(f"OCR_FAIL_DETAIL id={iid} err={e}")
                continue
            if lines:
                detail_ocr.append(" ".join(lines))
                if progress_cb:
                    progress_cb(f"OCR 详情图 {item.get('id')}: {len(lines)} 行")
    if main_urls and not main_ocr:
        hint = main_err[0] if main_err else "有图但识别结果为空"
        main_ocr.append(f"（主图 OCR 失败: {hint}）")
        _lg(f"OCR_EMPTY_MAIN id={iid} urls={len(main_urls)} err={main_err[:2]}")
    if detail_urls and not detail_ocr:
        hint = detail_err[0] if detail_err else "有图但识别结果为空"
        detail_ocr.append(f"（详情图 OCR 失败: {hint}）")
        _lg(f"OCR_EMPTY_DETAIL id={iid} urls={len(detail_urls)} err={detail_err[:2]}")
    out = dict(item)
    out["main_ocr"] = main_ocr
    out["detail_ocr"] = detail_ocr
    return out


def scan_item_local(
    item: dict,
    word_groups: list[tuple[str, list[str]]],
    *,
    do_ocr: bool = True,
    do_llm: bool = True,
    llm_conf: dict | None = None,
    main_ocr_count: int = 2,
    detail_ocr_count: int = 6,
    progress_cb: Callable[[str], None] | None = None,
) -> dict:
    """单品：本机 OCR + 词表 + 可选 LLM → problem markdown。"""
    iid = str(item.get("id") or "").strip()
    if not iid:
        raise ValueError("商品缺少 id")
    title = str(item.get("title") or iid).strip() or iid
    detail_texts = item.get("detail_texts") or []
    if isinstance(detail_texts, str):
        detail_text = detail_texts.strip()
    else:
        detail_text = "\n".join(str(x) for x in detail_texts if str(x).strip())

    work = dict(item)
    if do_ocr:
        work = _ocr_item(work, main_ocr_count, detail_ocr_count, progress_cb)
    main_ocr = [
        str(x).strip() for x in (work.get("main_ocr") or [])
        if str(x).strip() and not str(x).startswith("（主图 OCR 失败")
    ]
    detail_ocr = [
        str(x).strip() for x in (work.get("detail_ocr") or [])
        if str(x).strip() and not str(x).startswith("（详情图 OCR 失败")
    ]

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
    uniq: list[dict] = []
    for h in hits:
        k = (h.get("category", ""), h["source"], h["keyword"])
        if k not in seen:
            seen.add(k)
            uniq.append(h)

    judge = ""
    if do_llm and uniq and llm_conf:
        if progress_cb:
            progress_cb(f"LLM 判定 {iid}")
        judge = llm_judge(
            {"id": iid, "title": title, "url": f"https://item.taobao.com/item.htm?id={iid}"},
            uniq,
            llm_conf=llm_conf,
        )
    elif do_llm and uniq and not llm_conf:
        judge = "跳过 LLM：云端未下发完整 api 配置"

    kept = filter_hits_by_judge(uniq, judge) if judge else uniq
    # LLM 结构化判定后：全为假阳性则不写 problem（避免「最小规格」类误入库）
    problem = build_problem_md(kept, judge if kept else "")
    return {
        "id": iid,
        "title": title,
        "url": f"https://item.taobao.com/item.htm?id={iid}",
        "main_ocr": main_ocr,
        "detail_ocr": detail_ocr,
        "detail_texts": detail_texts if isinstance(detail_texts, list) else [detail_text] if detail_text else [],
        "hits": kept,
        "judge": judge,
        "problem": problem,
        "has_problem": bool(problem.strip()),
    }


def scan_items_local(
    items: list[dict],
    word_groups: list[tuple[str, list[str]]] | None = None,
    *,
    do_ocr: bool = True,
    do_llm: bool = True,
    llm_conf: dict | None = None,
    main_ocr_count: int = 2,
    detail_ocr_count: int = 6,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> list[dict]:
    groups = word_groups or load_local_word_groups()
    if not any(w for _, w in groups):
        raise RuntimeError(
            "本地词表为空：请在界面保存极限词/错误描述后再扫"
        )
    out: list[dict] = []
    total = len(items)
    for n, it in enumerate(items, 1):
        if not it.get("ok") and not (it.get("title") or it.get("main_image_urls")):
            out.append({
                "id": str(it.get("id") or ""),
                "title": str(it.get("title") or ""),
                "url": "",
                "hits": [],
                "judge": "",
                "problem": "",
                "has_problem": False,
                "error": it.get("error") or "抓取失败，跳过扫描",
            })
            if progress_cb:
                progress_cb(n, total, f"跳过失败商品 {it.get('id')}")
            continue

        def _p(msg: str, _n=n, _t=total) -> None:
            if progress_cb:
                progress_cb(_n, _t, msg)

        if progress_cb:
            progress_cb(n, total, f"本机扫描 {it.get('id')}")
        row = scan_item_local(
            it,
            groups,
            do_ocr=do_ocr,
            do_llm=do_llm,
            llm_conf=llm_conf,
            main_ocr_count=main_ocr_count,
            detail_ocr_count=detail_ocr_count,
            progress_cb=_p,
        )
        out.append(row)
        if progress_cb:
            flag = "有问题" if row["has_problem"] else "通过"
            progress_cb(n, total, f"[{flag}] {row['title'][:40]}")
    return out
