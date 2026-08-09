#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""店铺商品极限词扫描器 v2 — 标题 + 详情文本 + 主图OCR + 详情图OCR 全覆盖。

用法:
    python3 scanner.py                    # 全量扫描, 输出 Excel
    python3 scanner.py --skip-ocr         # 跳过图片 OCR(只扫文本)
    python3 scanner.py --max-items 10     # 只扫前 N 个商品(测试)

配置(config.ini):
    [words]  limit_file / wrong_file = 词表文件(空格/换行分隔)
    [scan]   items_file / output_xlsx / ocr_url
    [llm]    复用根目录 llm_config.py, 可选 --llm 二次判定
"""
from __future__ import annotations

import argparse
import configparser
import csv
import json
import re
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CFG = HERE / "config.ini"

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
except ImportError:  # pragma: no cover
    Workbook = None

TIMEOUT = 30


def load_cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(str(CFG), encoding="utf-8")
    return cfg


def _parse_words(text: str) -> list[str]:
    """词表按空白分隔（空格/换行/制表符），不再用 /。"""
    words: list[str] = []
    for piece in re.split(r"\s+", (text or "").strip()):
        piece = piece.strip()
        if piece and piece not in words:
            words.append(piece)
    return words


def _load_word_file(cfg: configparser.ConfigParser, option: str, fallback: str) -> list[str]:
    rel = cfg.get("words", option, fallback=fallback).strip()
    path = HERE / rel
    if not path.is_file():
        return []
    return _parse_words(path.read_text(encoding="utf-8"))


def load_word_groups(cfg: configparser.ConfigParser) -> list[tuple[str, list[str]]]:
    """加载极限词 + 错误描述两组词表。"""
    return [
        ("极限词", _load_word_file(cfg, "limit_file", "file/absolute_words.md")),
        ("错误描述", _load_word_file(cfg, "wrong_file", "file/wrong_word.md")),
    ]


def load_words(cfg: configparser.ConfigParser) -> list[str]:
    """合并两组词表（兼容旧调用）。"""
    words: list[str] = []
    for _label, group in load_word_groups(cfg):
        for w in group:
            if w not in words:
                words.append(w)
    if not words:
        sys.exit("词表为空：请检查 file/absolute_words.md 与 file/wrong_word.md")
    return words

def load_items(cfg: configparser.ConfigParser) -> list[dict]:
    items_file = cfg.get("scan", "items_file", fallback="data/items.csv")
    path = HERE / items_file
    if not path.is_file():
        sys.exit(f"商品数据文件不存在: {path}")
    items: list[dict] = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            items.append({
                "id": row.get("id", "").strip(),
                "title": row.get("title", "").strip(),
                "url": row.get("url", "").strip() or f"https://item.taobao.com/item.htm?id={row.get('id','').strip()}",
            })
    return items


def hit_context(text: str, keyword: str, radius: int = 10) -> str:
    idx = text.find(keyword)
    if idx < 0:
        return ""
    start = max(0, idx - radius)
    end = min(len(text), idx + len(keyword) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def scan_texts(words: list[str], texts: dict[str, str], category: str = "") -> list[dict]:
    """对 {来源: 文本} 做词表扫描, 返回命中列表。"""
    hits: list[dict] = []
    for source, text in texts.items():
        if not text:
            continue
        for w in words:
            if w in text:
                hit = {
                    "source": source,
                    "keyword": w,
                    "context": hit_context(text, w),
                }
                if category:
                    hit["category"] = category
                hits.append(hit)
    return hits


def default_ocr_base(cfg: configparser.ConfigParser | None = None) -> str:
    if cfg is None:
        cfg = load_cfg()
    return cfg.get("ocr", "ocr_base", fallback="http://127.0.0.1:8799").rstrip("/")


def ocr_url(img_url: str, ocr_base: str = "") -> list[str]:
    """调服务器 OCR 服务识别图片文字。失败返回空。"""
    base = (ocr_base or default_ocr_base()).rstrip("/")
    try:
        r = requests.post(f"{base}/ocr_url", json={"url": img_url}, timeout=TIMEOUT + 20)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            return []
        return data.get("texts", [])
    except Exception:  # noqa: BLE001
        return []


def local_images(item_id: str) -> list[Path]:
    """返回本地 images/item_{id}/ 下的图片(离线OCR用, 不依赖网络)。"""
    d = HERE / "images" / f"item_{item_id}"
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif"))


def ocr_local_file(img_path: Path, ocr_base: str = "") -> list[str]:
    """把本地图片 POST 到服务器 OCR(multipart)。失败返回空。"""
    base = (ocr_base or default_ocr_base()).rstrip("/")
    try:
        with img_path.open("rb") as f:
            r = requests.post(
                f"{base}/ocr",
                files={"file": (img_path.name, f, "image/jpeg")},
                timeout=TIMEOUT + 20,
            )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            return []
        return data.get("texts", [])
    except Exception:  # noqa: BLE001
        return []


def _extract_json_obj(text: str) -> dict | None:
    """从模型输出中稳健提取 JSON 对象（避免贪婪正则把后续杂文吃进去）。"""
    raw = (text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(raw[i:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def llm_judge(item: dict, hits: list[dict], rules_text: str = "") -> str:
    """LLM 二次判定(可选): 汇总命中项判断是否真违规。返回结论文本。"""
    from llm_config import call_chat

    lines = [f"商品标题: {item['title']}", f"链接: {item['url']}", ""]
    for h in hits:
        cat = h.get("category") or "候选"
        lines.append(f"- [{cat}/{h['source']}] 命中「{h['keyword']}」 上下文: {h['context']}")
    prompt = (
        "你是电商广告合规审核员。下面商品文本命中了极限词或错误描述候选, "
        "请判断哪些构成《广告法》绝对化用语违规或虚假宣传, 哪些是正常表述。\n\n"
        + "\n".join(lines)
        + "\n\n"
        + (f"《规则文档》\n{rules_text}\n\n" if rules_text else "")
        + "只输出一个 JSON 对象，不要 markdown，不要解释。"
        + '格式: {"violations":[{"keyword":"...","source":"...","violate":true,"reason":"..."}]}'
    )
    try:
        out = call_chat(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            timeout=90,
            config_path=CFG,
        )
        obj = _extract_json_obj(out)
        if obj is not None:
            return json.dumps(obj, ensure_ascii=False)
        return out[:500]
    except Exception as e:  # noqa: BLE001
        return f"LLM调用失败: {e}"


def write_xlsx(results: list[dict], out_path: Path):
    if Workbook is None:  # pragma: no cover
        raise RuntimeError("缺少 openpyxl, 请: .venv-abs/bin/pip install openpyxl")
    wb = Workbook()
    ws = wb.active
    ws.title = "违规清单"
    headers = ["序号", "店铺/商品ID", "标题", "链接", "违规来源", "命中词", "上下文", "详情页OCR文本", "判定"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="FFF2CC")
    red = PatternFill("solid", fgColor="FFC7CE")
    for i, r in enumerate(results, 1):
        row = [
            i,
            r["id"],
            r["title"],
            r["url"],
            "、".join({h["source"] for h in r["hits"]}),
            "、".join({h["keyword"] for h in r["hits"]}),
            " || ".join({h["context"] for h in r["hits"]}),
            r["ocr_summary"],
            r.get("judge", ""),
        ]
        ws.append(row)
        if r["hits"]:
            for c in ws[ws.max_row]:
                c.fill = red
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 55
    ws.column_dimensions["D"].width = 45
    ws.column_dimensions["E"].width = 16
    ws.column_dimensions["F"].width = 20
    ws.column_dimensions["G"].width = 60
    ws.column_dimensions["H"].width = 80
    ws.column_dimensions["I"].width = 50
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    wb.save(out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="店铺商品极限词扫描 v2")
    parser.add_argument("--skip-ocr", action="store_true", help="跳过图片 OCR")
    parser.add_argument("--max-items", type=int, default=0, help="只扫前 N 个商品")
    parser.add_argument("--llm", action="store_true", help="LLM 二次判定")
    args = parser.parse_args()

    cfg = load_cfg()
    word_groups = load_word_groups(cfg)
    words = load_words(cfg)
    items = load_items(cfg)
    if args.max_items:
        items = items[: args.max_items]
    ocr_base = cfg.get("ocr", "ocr_base", fallback=default_ocr_base(cfg))

    for label, group in word_groups:
        print(f"{label}({len(group)}): {group}")
    print(f"商品数: {len(items)}  OCR: {'跳过' if args.skip_ocr else ocr_base}")

    results: list[dict] = []
    total_hits = 0
    for n, it in enumerate(items, 1):
        print(f"[{n}/{len(items)}] {it['id']} {it['title'][:30]}...")
        texts: dict[str, str] = {"标题": it["title"]}
        ocr_texts: list[str] = []

        # 抓详情(标题/详情文本/图URL) — 无 cookie 时跳过详情, 只用标题
        from fetch_item import fetch_item

        detail = fetch_item(it["id"])
        if detail.get("title"):
            texts["标题"] = detail["title"]
        if detail.get("detail_texts"):
            texts["详情文本"] = "\n".join(detail["detail_texts"])
        if detail.get("error"):
            print(f"  ⚠ {detail['error']}")

        # 图片 OCR: 优先本地已下载图, 否则主图/详情图URL
        if not args.skip_ocr:
            local = local_images(it["id"])
            if local:
                for p in local:
                    t = ocr_local_file(p, ocr_base)
                    if t:
                        ocr_texts.extend(t)
                        print(f"  OCR[本地] {p.name[:40]}: {len(t)} 行")
            else:
                all_imgs = detail.get("main_image_urls", [])[:4] + detail.get("detail_image_urls", [])[:12]
                for u in all_imgs:
                    t = ocr_url(u, ocr_base)
                    if t:
                        ocr_texts.extend(t)
                        print(f"  OCR {u.rsplit('/', 1)[-1][:40]}: {len(t)} 行")
            if ocr_texts:
                texts["图片文字"] = "\n".join(ocr_texts)

        hits: list[dict] = []
        for category, group in word_groups:
            if group:
                hits.extend(scan_texts(group, texts, category=category))
        # 去重
        seen = set()
        uniq_hits = []
        for h in hits:
            k = (h.get("category", ""), h["source"], h["keyword"])
            if k not in seen:
                seen.add(k)
                uniq_hits.append(h)
        if uniq_hits:
            total_hits += len(uniq_hits)
        judge = ""
        if args.llm and uniq_hits:
            rules = ""
            rules_file = HERE / "rules.md"
            if rules_file.is_file():
                rules = rules_file.read_text(encoding="utf-8")
            judge = llm_judge(it, uniq_hits, rules)
            print(f"  LLM: {judge[:150]}")

        results.append({
            "id": it["id"],
            "title": texts["标题"],
            "url": it["url"],
            "hits": uniq_hits,
            "ocr_summary": "\n".join(ocr_texts)[:500],
            "judge": judge,
        })

    out_path = HERE / cfg.get("scan", "output_xlsx", fallback="output/违规清单.xlsx")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(results, out_path)

    n_violated = sum(1 for r in results if r["hits"])
    print(f"\n完成: {len(results)} 个商品, {n_violated} 个有命中, 共 {total_hits} 处")
    print(f"Excel: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
