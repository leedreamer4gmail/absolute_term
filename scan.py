#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""广告极限词扫描器：标题扫描 + 图片 OCR 文字扫描 → LLM 语义判定 → 输出报告。

用法:
    python3 scan.py                  # 仅标题词表扫描(快)
    python3 scan.py --ocr            # 标题 + 图片 OCR 扫描
    python3 scan.py --ocr --llm      # 标题 + OCR + LLM 语义判定(慢)
"""
from __future__ import annotations

import argparse
import configparser
import csv
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = HERE / "config.ini"
ROOT = HERE.parent  # 公司根目录


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


def load_words(cfg: configparser.ConfigParser) -> list[str]:
    """读极限词 + 错误描述两组词表并合并。"""
    words: list[str] = []
    for option, fallback in (
        ("limit_file", "file/absolute_words.md"),
        ("wrong_file", "file/wrong_word.md"),
    ):
        rel = cfg.get("words", option, fallback=fallback).strip()
        path = HERE / rel
        if not path.is_file():
            continue
        for w in _parse_words(path.read_text(encoding="utf-8")):
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
            items.append({"id": row.get("id", "").strip(), "title": row.get("title", "").strip()})
    return items


def hit_context(text: str, keyword: str, radius: int = 8) -> str:
    """截取命中词上下文片段。"""
    idx = text.find(keyword)
    if idx < 0:
        return ""
    start = max(0, idx - radius)
    end = min(len(text), idx + len(keyword) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def scan_keywords(words: list[str], items: list[dict]) -> list[dict]:
    """词表正则扫描标题, 返回命中列表。"""
    hits: list[dict] = []
    for it in items:
        title = it["title"]
        for w in words:
            if w in title:
                hits.append({
                    "id": it["id"],
                    "source": "title",
                    "title": title,
                    "keyword": w,
                    "context": hit_context(title, w),
                })
    return hits


def scan_images_ocr(words: list[str], cfg: configparser.ConfigParser) -> list[dict]:
    """扫描图片目录中所有商品图片，OCR 提取文字后匹配词表。"""
    image_dir = cfg.get("image", "image_dir", fallback="images")
    conf_threshold = float(cfg.get("ocr", "confidence", fallback="0.5"))
    img_path = HERE / image_dir
    if not img_path.is_dir():
        print(f"  [跳过] 图片目录不存在: {img_path}")
        return []

    try:
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR()
    except ImportError:
        print("  [错误] 未安装 rapidocr-onnxruntime，跳过 OCR")
        return []

    hits: list[dict] = []
    item_dirs = sorted(img_path.iterdir())

    for item_dir in item_dirs:
        if not item_dir.is_dir():
            continue
        item_id = item_dir.name.replace("item_", "")
        images = sorted(item_dir.glob("*"))
        if not images:
            continue

        # 提前知道商品标题
        item_title = ""
        items_file = cfg.get("scan", "items_file", fallback="data/items.csv")
        csv_path = HERE / items_file
        if csv_path.is_file():
            with csv_path.open(encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("id", "").strip() == item_id:
                        item_title = row.get("title", "").strip()
                        break

        for img_file in images:
            try:
                result, _ = engine(str(img_file))
            except Exception:
                continue
            if not result:
                continue
            line_text = ""
            for box, text, score in result:
                try:
                    fs = float(score)
                except (TypeError, ValueError):
                    fs = 0
                if fs >= conf_threshold:
                    line_text += text
            for w in words:
                if w in line_text:
                    hits.append({
                        "id": item_id,
                        "source": "image_ocr",
                        "title": item_title,
                        "keyword": w,
                        "context": hit_context(line_text, w),
                        "ocr_text": line_text[:200],
                        "image_file": str(img_file.relative_to(HERE)),
                    })
    return hits


def llm_judge(hits: list[dict]) -> list[dict]:
    """LLM 模糊语义判定: 命中项是否真违规。"""
    from llm_config import call_chat

    judged: list[dict] = []
    for h in hits:
        if h["source"] == "title":
            prompt = (
                "你是电商广告合规审核员。商品标题命中了极限词候选, 请判断是否构成"
                "《广告法》禁止的绝对化用语违规。\n"
                f"商品标题: {h['title']}\n"
                f"命中关键词: {h['keyword']}\n"
                f"命中上下文: {h['context']}\n"
                "输出 JSON: {\"status\": \"pass|fail\", \"reason\": \"判断依据\"}\n"
                "注意: '最近''最后''最新''最佳食用方式'等非广告宣称不算违规; "
                "指向自身品质的绝对化宣称(如'最顶级''天花板''首创')算违规。"
            )
        else:
            prompt = (
                "你是电商广告合规审核员。商品图片文字命中了极限词候选, 请判断是否构成"
                "《广告法》禁止的绝对化用语违规。\n"
                f"商品ID: {h['id']}\n"
                f"商品标题: {h['title']}\n"
                f"命中关键词: {h['keyword']}\n"
                f"命中上下文: {h['context']}\n"
                f"OCR 原文: {h.get('ocr_text', '')}\n"
                "输出 JSON: {\"status\": \"pass|fail\", \"reason\": \"判断依据\"}\n"
                "注意: 俗语(如'最香不过贴骨肉')和客观研发陈述(如'耗时最久')不算违规; "
                "指向商品品质的绝对化宣称(如'天花板''首创''最好')算违规。"
            )
        try:
            out = call_chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                timeout=60,
            )
            m = re.search(r"\{.*\}", out, re.S)
            parsed = {}
            if m:
                parsed = json.loads(m.group(0))
            h["llm_status"] = parsed.get("status", "unknown")
            h["llm_reason"] = parsed.get("reason", out[:200])
        except Exception as e:  # noqa: BLE001
            h["llm_status"] = "error"
            h["llm_reason"] = str(e)
        judged.append(h)
    return judged


def output_report(hits: list[dict], words: list[str], items: list[dict], llm_used: bool, ocr_used: bool) -> Path:
    output_file = load_cfg().get("scan", "output_file", fallback="output/违规清单.txt")
    out_path = HERE / output_file
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"广告极限词扫描报告",
        f"词表: {'/'.join(words)}  商品数: {len(items)}  总命中: {len(hits)}",
        f"扫描模式: {'OCR图片+' if ocr_used else ''}标题 {'+LLM判定' if llm_used else ''}",
        "=" * 70,
    ]

    title_hits = [h for h in hits if h["source"] == "title"]
    ocr_hits = [h for h in hits if h["source"] == "image_ocr"]

    if title_hits:
        lines.append(f"\n【标题命中】{len(title_hits)} 条")
        lines.append("-" * 70)
        for h in title_hits:
            extra = ""
            if llm_used:
                extra = f"  [LLM:{h.get('llm_status')}] {h.get('llm_reason', '')}"
            lines.append(f"商品ID: {h['id']}")
            lines.append(f"标题:   {h['title']}")
            lines.append(f"命中:   {h['keyword']}  上下文: {h['context']}{extra}")
            lines.append("-" * 70)
    else:
        lines.append(f"\n【标题命中】0 条 ✅")
        lines.append("-" * 70)

    if ocr_hits:
        lines.append(f"\n【图片 OCR 命中】{len(ocr_hits)} 条")
        lines.append("-" * 70)
        for h in ocr_hits:
            extra = ""
            if llm_used:
                extra = f"  [LLM:{h.get('llm_status')}] {h.get('llm_reason', '')}"
            lines.append(f"商品ID: {h['id']}")
            lines.append(f"标题:   {h['title']}")
            lines.append(f"命中:   {h['keyword']}  上下文: {h['context']}{extra}")
            lines.append(f"图片:   {h.get('image_file', '')}")
            lines.append("-" * 70)
    else:
        lines.append(f"\n【图片 OCR 命中】0 条 ✅")
        lines.append("-" * 70)

    lines.append("")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="广告极限词扫描")
    parser.add_argument("--ocr", action="store_true", help="启用图片 OCR 文字扫描")
    parser.add_argument("--llm", action="store_true", help="命中项做 LLM 模糊语义判定")
    args = parser.parse_args()

    cfg = load_cfg()
    words = load_words(cfg)
    items = load_items(cfg)

    print(f"词表({len(words)}): {words}")
    print(f"商品数: {len(items)}")

    # 标题扫描
    hits = scan_keywords(words, items)
    print(f"标题扫描命中: {len(hits)} 条")

    # 图片 OCR 扫描
    if args.ocr:
        print("开始图片 OCR 扫描...")
        ocr_hits = scan_images_ocr(words, cfg)
        print(f"图片 OCR 命中: {len(ocr_hits)} 条")
        hits.extend(ocr_hits)

    # LLM 语义判定
    if args.llm and hits:
        print(f"LLM 语义判定 ({len(hits)} 条)...")
        hits = llm_judge(hits)

    out_path = output_report(hits, words, items, args.llm, args.ocr)
    print(f"结果已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())