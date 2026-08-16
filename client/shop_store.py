#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 file/<用户名>/shops/ 一店一 md：新商品追加，已有（同 id/标题）不重复写。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_SHOPS_DIR: Path | None = None
_CURRENT_USER = ""

_JUNK_TITLES = frozenset({
    "登录",
    "注册",
    "请登录",
    "亲，请登录",
    "淘宝网",
    "天猫",
    "验证",
    "安全验证",
    "滑块验证",
    "登录 - 淘宝网",
    "登录-淘宝网",
    "主图内容",
    "详情内容",
})


def app_user_dir() -> Path:
    """日志 / Chrome 配置 / 账号 ini。源文件和 Excel 不走这里，走 program file/<用户名>/。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        p = Path(base) / "小李的电商扫描器"
    else:
        p = Path.home() / ".xiaoli_scanner"
    p.mkdir(parents=True, exist_ok=True)
    return p


def install_root() -> Path:
    """程序根：源码=仓库根；安装包=exe 所在目录（不是 _internal）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return ROOT


def program_file_dir() -> Path:
    return install_root() / "file"


def safe_username(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip())
    s = re.sub(r"\s+", "_", s).strip(" .")
    return s[:80]


def set_current_username(name: str) -> str:
    global _CURRENT_USER
    _CURRENT_USER = safe_username(name)
    return _CURRENT_USER


def current_username() -> str:
    return _CURRENT_USER


def user_file_dir(username: str | None = None) -> Path:
    """file/<用户名>/。未登录用 _nologin，禁止去读别人的目录。"""
    u = safe_username(username if username is not None else _CURRENT_USER) or "_nologin"
    p = program_file_dir() / u
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"无法创建 {p}。请把软件装到可写位置（不要装进 Program Files）: {e}"
        ) from e
    return p


def default_shops_dir(username: str | None = None) -> Path:
    p = user_file_dir(username) / "shops"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"无法创建店铺目录 {p}: {e}") from e
    return p


def default_log_path() -> Path:
    return app_user_dir() / "client.log"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """安装包只读资源（PyInstaller _MEIPASS/_internal）；源码运行是仓库根。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(str(meipass))
    return ROOT


def uses_user_data() -> bool:
    """安装包 / Program Files：可写文件必须进用户目录，禁止写安装树。"""
    return is_frozen() or _is_install_tree(ROOT)


def writable_client_dir() -> Path:
    """账号、布局、Cookie、Chrome 配置的可写根。源码运行仍用 client/。"""
    if uses_user_data():
        return app_user_dir()
    return HERE


def writable_data_dir() -> Path:
    p = writable_client_dir() / "data"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"无法创建数据目录 {p}: {e}") from e
    return p


def client_ini_path() -> Path:
    """本机账号/布局 ini。安装包禁止写 _internal/config.ini（那是应用配置）。"""
    return writable_client_dir() / "config.ini"


def cookie_file_path() -> Path:
    return writable_data_dir() / "cookie.txt"


def default_output_dir(username: str | None = None) -> Path:
    p = user_file_dir(username) / "output"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"无法创建 Excel 输出目录 {p}: {e}") from e
    return p


def is_auto_managed_path(path: str | Path | None) -> bool:
    """空路径、旧 AppData、以及 file/<用户>/shops|output：登录换人时自动切，不当成用户手选。"""
    raw = "" if path is None else str(path).strip()
    if not raw:
        return True
    try:
        p = Path(raw).resolve()
    except OSError:
        return True
    text = str(p).replace("/", "\\").lower()
    if "\\appdata\\" in text:
        return True
    try:
        rel = p.relative_to(program_file_dir().resolve())
    except ValueError:
        return False
    parts = [x.lower() for x in rel.parts]
    return len(parts) >= 2 and parts[-1] in ("shops", "output")


def chrome_profile_dir() -> Path:
    p = writable_data_dir() / "chrome_profile"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"无法创建 Chrome 配置目录 {p}: {e}") from e
    return p


def link_queue_path() -> Path:
    return writable_data_dir() / "link_queue.json"


def bundled_path(*parts: str) -> Path:
    return bundle_dir().joinpath(*parts)


def app_config_ini_paths() -> list[Path]:
    """只读应用配置（[ui]/[client] 等），不是账号 ini。"""
    paths = [bundle_dir() / "config.ini", HERE / "config.ini", ROOT / "config.ini"]
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p).replace("\\", "/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def word_file_paths() -> tuple[Path, Path]:
    """可编辑词表。安装版首次从包内复制到用户目录；源码用仓库 file/。"""
    names = ("absolute_words.md", "wrong_word.md")
    if uses_user_data():
        dst_dir = app_user_dir() / "file"
        found: list[Path] = []
        missing: list[str] = []
        for name in names:
            dst = dst_dir / name
            src = bundled_path("file", name)
            if not dst.is_file():
                if src.is_file():
                    try:
                        dst_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                    except OSError as e:
                        raise RuntimeError(f"无法写入词表 {dst}: {e}") from e
                else:
                    missing.append(str(src))
            found.append(dst)
        if missing:
            raise FileNotFoundError(
                "安装包缺少词表文件，新用户无法扫描: " + " ; ".join(missing)
            )
        return found[0], found[1]
    return ROOT / "file" / names[0], ROOT / "file" / names[1]


def _is_install_tree(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    text = str(resolved).replace("/", "\\").lower()
    markers = ("\\program files", "\\program files (x86)")
    if any(m in text for m in markers):
        return True
    if getattr(sys, "frozen", False):
        return True
    return False


def get_shops_dir() -> Path:
    return _SHOPS_DIR if _SHOPS_DIR is not None else default_shops_dir()


def set_shops_dir(path: str | Path | None) -> Path:
    """设置当前账号的店铺 md 目录。空则 file/<用户名>/shops。写不进去就报错，不改去 AppData。"""
    global _SHOPS_DIR
    raw = "" if path is None else str(path).strip()
    if not raw or is_auto_managed_path(raw):
        p = default_shops_dir()
    else:
        p = Path(raw)
        if not p.is_absolute():
            p = (install_root() / p).resolve()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"无法创建店铺目录 {p}: {e}") from e
    _SHOPS_DIR = p
    return _SHOPS_DIR


def is_junk_product_title(title: str) -> bool:
    """登录页/验证页标题，不能当商品名。"""
    t = (title or "").strip()
    if not t:
        return True
    if t in _JUNK_TITLES:
        return True
    compact = re.sub(r"[\s\-—_|·]+", "", t)
    if compact in ("登录", "请登录", "登录淘宝网", "淘宝网", "亲请登录"):
        return True
    return False


def shop_md_search_dirs() -> list[Path]:
    """只认当前登录用户的目录，禁止扫到别人的 md。"""
    return [get_shops_dir()]


def _safe_name(name: str) -> str:
    s = (name or "").strip() or "未命名店铺"
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:80] or "未命名店铺")


def shop_md_path(shop_name: str) -> Path:
    return get_shops_dir() / f"{_safe_name(shop_name)}.md"


def try_local_shop_md(shop_name: str) -> Path | None:
    """本地已有源文件则返回路径，没有返回 None（不报错）。"""
    name = f"{_safe_name(shop_name)}.md"
    for d in shop_md_search_dirs():
        path = d / name
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def list_local_shop_mds() -> dict[str, Path]:
    """本地全部店铺 md：stem 小写 → 路径。用户目录优先，同名不覆盖。"""
    found: dict[str, Path] = {}
    for d in shop_md_search_dirs():
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            if not p.is_file() or p.stat().st_size <= 0:
                continue
            key = p.stem.lower()
            if key not in found:
                found[key] = p
    return found


def match_local_shop_md(shop: dict, local_index: dict[str, Path] | None = None) -> Path | None:
    """按店名 / 淘宝店 id 在本地 md 里找文件。"""
    idx = local_index if local_index is not None else list_local_shop_mds()
    names = [
        str(shop.get("shop_name") or "").strip(),
        str(shop.get("tb_shop_id") or shop.get("shop_id") or "").strip(),
    ]
    for raw in names:
        if not raw:
            continue
        hit = idx.get(raw.lower()) or idx.get(_safe_name(raw).lower())
        if hit is not None:
            return hit
    name = names[0] or names[1]
    return try_local_shop_md(name) if name else None


def write_shop_md_text(shop_name: str, text: str) -> Path:
    """把云端源文件落到本机 shops 目录。空内容直接报错。"""
    body = text if text is not None else ""
    if not str(body).strip():
        raise ValueError("云端源文件为空，无法写入本地")
    get_shops_dir().mkdir(parents=True, exist_ok=True)
    path = shop_md_path(shop_name)
    try:
        path.write_text(str(body), encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"无法写入本地源文件 {path}: {e}") from e
    return path


def _item_key(it: dict) -> str:
    iid = str(it.get("id") or it.get("tb_item_id") or "").strip()
    if iid:
        return f"id:{iid}"
    title = str(it.get("title") or "").strip()
    if title:
        return f"title:{title}"
    raw = render_item_block(it)
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def render_item_block(it: dict) -> str:
    """单商品 md 块（含 item_id 标记，便于去重）。"""
    iid = str(it.get("id") or it.get("tb_item_id") or "").strip()
    title = str(it.get("title") or iid or "商品").strip()
    lines: list[str] = []
    if iid:
        lines.append(f"<!-- item_id: {iid} -->")
    lines.append(f"# {title}")
    lines.append("# 主图内容")
    main_ocr = it.get("main_ocr") or []
    if main_ocr:
        for i, line in enumerate(main_ocr, 1):
            lines.append(f"    - 主图{i}：{str(line).strip()}")
    else:
        lines.append("    - （无主图 OCR）")
    lines.append("# 详情内容")
    detail_bits: list[str] = []
    texts = it.get("detail_texts") or []
    if isinstance(texts, str) and texts.strip():
        detail_bits.append(texts.strip())
    elif isinstance(texts, list):
        detail_bits.extend(str(x).strip() for x in texts if str(x).strip())
    for line in it.get("detail_ocr") or []:
        if str(line).strip():
            detail_bits.append(str(line).strip())
    if detail_bits:
        lines.append("    " + "\n    ".join(detail_bits))
    else:
        lines.append("    （无详情文本）")
    lines.append("")
    return "\n".join(lines)


def existing_keys(path: Path) -> set[str]:
    """从已有 md 解析已记录的商品 id / 标题。"""
    keys: set[str] = set()
    if not path.is_file():
        return keys
    text = path.read_text(encoding="utf-8")
    for m in re.finditer(r"<!--\s*item_id:\s*(\S+)\s*-->", text):
        keys.add(f"id:{m.group(1).strip()}")
    for m in re.finditer(r"(?m)^#\s+(.+?)\s*$", text):
        t = m.group(1).strip()
        if t in ("主图内容", "详情内容"):
            continue
        keys.add(f"title:{t}")
    return keys


def save_shop_md_stats(
    shop_name: str,
    items: list[dict],
    *,
    shop_link: str = "",
) -> dict:
    """合并写入 <店名>.md：重复跳过，新的追加。"""
    get_shops_dir().mkdir(parents=True, exist_ok=True)
    path = shop_md_path(shop_name)
    have = existing_keys(path)
    added = 0
    skipped = 0
    added_blocks: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or it.get("tb_item_id") or "").strip()
        title = str(it.get("title") or "").strip()
        if title and is_junk_product_title(title):
            skipped += 1
            continue
        key = _item_key(it)
        if key in have or (iid and f"id:{iid}" in have) or (title and f"title:{title}" in have):
            skipped += 1
            continue
        added_blocks.append(render_item_block(it))
        added += 1
        have.add(key)
        if iid:
            have.add(f"id:{iid}")
        if title:
            have.add(f"title:{title}")

    if not path.is_file():
        header = f"<!-- shop_link: {shop_link} -->\n" if shop_link else ""
        path.write_text(header + "".join(added_blocks), encoding="utf-8")
    elif added_blocks:
        prev = path.read_text(encoding="utf-8")
        sep = "" if prev.endswith("\n") else "\n"
        path.write_text(prev + sep + "".join(added_blocks), encoding="utf-8")

    return {
        "path": path,
        "added": added,
        "skipped": skipped,
        "file_exists": path.is_file(),
    }


def save_shop_md(
    shop_name: str,
    items: list[dict],
    *,
    shop_link: str = "",
) -> Path:
    """兼容旧调用。"""
    return save_shop_md_stats(shop_name, items, shop_link=shop_link)["path"]


def overwrite_shop_md(
    shop_name: str,
    items: list[dict],
    *,
    shop_link: str = "",
) -> dict:
    """重新扫描：覆盖写入整店 md（先删旧文件再写）。"""
    path = shop_md_path(shop_name)
    if path.is_file():
        path.unlink()
    return save_shop_md_stats(shop_name, items, shop_link=shop_link)


def parse_shop_md(path: Path) -> dict:
    """把本地店铺 md 解析成可分析的商品列表。

    返回 {shop_name, shop_link, items:[{id,title,main_ocr,detail_texts,url}]}。
    文件不存在或无商品时直接报错。
    """
    if not path.is_file():
        raise FileNotFoundError(f"本地店铺文件不存在: {path}")
    text = path.read_text(encoding="utf-8")
    shop_link = ""
    m_link = re.search(r"<!--\s*shop_link:\s*(.*?)\s*-->", text)
    if m_link:
        shop_link = m_link.group(1).strip()

    shop_name = path.stem
    # 按 item_id 注释切块；兼容无注释的旧文件（按标题 # 切）
    chunks: list[tuple[str, str]] = []
    parts = re.split(r"(?=<!--\s*item_id:\s*\S+\s*-->)", text)
    for part in parts:
        part = part.strip()
        if not part or part.startswith("<!-- shop_link:"):
            continue
        m_id = re.match(r"<!--\s*item_id:\s*(\S+)\s*-->\s*", part)
        iid = m_id.group(1).strip() if m_id else ""
        body = part[m_id.end() :] if m_id else part
        if not body.strip():
            continue
        chunks.append((iid, body))

    if not chunks:
        # 回退：按顶级标题切
        blocks = re.split(r"(?m)(?=^#\s+(?!主图内容|详情内容))", text)
        for body in blocks:
            body = body.strip()
            if not body or body.startswith("<!-- shop_link:"):
                continue
            chunks.append(("", body))

    items: list[dict] = []
    for iid, body in chunks:
        title = ""
        m_title = re.search(r"(?m)^#\s+(.+?)\s*$", body)
        if m_title:
            t = m_title.group(1).strip()
            if t not in ("主图内容", "详情内容"):
                title = t
        main_ocr: list[str] = []
        for m in re.finditer(r"(?m)^\s*-\s*主图\d+：\s*(.*)$", body):
            line = m.group(1).strip()
            if line and line not in ("（无主图 OCR）",) and not line.startswith("（主图 OCR 失败"):
                main_ocr.append(line)
        detail_texts: list[str] = []
        m_detail = re.search(
            r"(?ms)^#\s*详情内容\s*\n(.*?)(?=^#\s|\Z)",
            body,
        )
        if m_detail:
            raw = m_detail.group(1)
            for line in raw.splitlines():
                s = line.strip()
                if not s or s in ("（无详情文本）",) or s.startswith("（详情图 OCR 失败"):
                    continue
                # 去掉块内缩进前缀
                s = re.sub(r"^-\s*", "", s)
                detail_texts.append(s)
        if not iid and not title:
            continue
        if title and is_junk_product_title(title):
            continue
        use_id = iid or title
        items.append(
            {
                "id": use_id,
                "tb_item_id": use_id,
                "title": title or use_id,
                "main_ocr": main_ocr,
                "detail_ocr": [],
                "detail_texts": detail_texts,
                "url": f"https://item.taobao.com/item.htm?id={iid}" if iid.isdigit() else "",
                "ok": True,
            }
        )

    if not items:
        raise ValueError(f"店铺文件无可分析商品: {path}")
    return {
        "shop_name": shop_name,
        "shop_link": shop_link,
        "path": path,
        "items": items,
    }


def find_local_shop_md(shop_name: str) -> Path:
    name = f"{_safe_name(shop_name)}.md"
    tried: list[Path] = []
    for d in shop_md_search_dirs():
        path = d / name
        tried.append(path)
        if path.is_file():
            return path
    lines = ["本地还没有这家店的扫描文件。"]
    lines.append("已找过：")
    for p in tried:
        lines.append(f"  {p}")
    existing: list[str] = []
    for d in shop_md_search_dirs():
        if d.is_dir():
            existing.extend(sorted(x.name for x in d.glob("*.md")))
    if existing:
        shown = "、".join(existing[:12])
        extra = f" 等 {len(existing)} 个" if len(existing) > 12 else ""
        lines.append(f"当前目录里有：{shown}{extra}")
    lines.append("请对该店点「重新扫描」。源文件保存在本机用户目录，不跟安装包走。")
    raise FileNotFoundError("\n".join(lines))
