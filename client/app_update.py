#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端检查更新 / 下载安装（精简自 livestream core/app_update）。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from version import CLIENT_VERSION

ProgressCb = Callable[[str, float | None], None]


@dataclass(frozen=True)
class AppUpdateInfo:
    current: str
    remote: str
    url: str
    label: str
    note: str
    update_available: bool


def _parse_version(text: str) -> tuple[int, ...]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("版本号为空")
    m = re.match(r"^(\d+(?:\.\d+)*)", raw)
    if not m:
        raise ValueError(f"无法解析版本号: {text!r}")
    parts = tuple(int(p) for p in m.group(1).split("."))
    if not parts:
        raise ValueError(f"无法解析版本号: {text!r}")
    return parts


def version_is_newer(remote: str, local: str) -> bool:
    return _parse_version(remote) > _parse_version(local)


def fetch_app_update_info(server: str, *, timeout: float = 20.0) -> AppUpdateInfo:
    current = (CLIENT_VERSION or "").strip() or "0.0.0"
    base = (server or "").rstrip("/")
    if not base:
        raise ValueError("server 不能为空")
    url = f"{base}/client/app-update?current={urllib.parse.quote(current)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"检查更新失败 HTTP {exc.code}: {body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"检查更新失败：无法连接云端（{exc.reason}）") from exc
    data = json.loads(raw)
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"检查更新响应无效: {raw[:200]}")
    remote = str(data.get("version") or "").strip()
    download = str(data.get("url") or "").strip()
    if not remote:
        raise RuntimeError("云端未配置 client_app_version")
    if not download:
        raise RuntimeError("云端未配置 download_update_url")
    if "update_available" in data:
        available = bool(data.get("update_available"))
    else:
        available = version_is_newer(remote, current)
    return AppUpdateInfo(
        current=current,
        remote=remote,
        url=download,
        label=str(data.get("label") or "客户端").strip() or "客户端",
        note=str(data.get("note") or "").strip(),
        update_available=available,
    )


def _download_url_to_path(
    url: str,
    target: Path,
    *,
    progress: ProgressCb | None = None,
    timeout: float = 300.0,
) -> None:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = resp.headers.get("Content-Length")
        total_n = int(total) if total and str(total).isdigit() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        done = 0
        last_pct = -1
        with target.open("wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    if total_n:
                        frac = min(1.0, done / total_n)
                        pct = int(frac * 100)
                        if pct != last_pct or done >= total_n:
                            last_pct = pct
                            progress(f"下载中 {done}/{total_n}", frac)
                    else:
                        progress(f"下载中 {done} 字节", None)


def download_and_launch_update(
    info: AppUpdateInfo,
    *,
    progress: ProgressCb | None = None,
) -> Path:
    if not info.update_available:
        raise RuntimeError("当前已是最新版本，无需下载")
    work = Path(tempfile.mkdtemp(prefix="absolute_term_update_"))
    name = Path(urllib.parse.urlparse(info.url).path).name or "update.bin"
    target = work / name
    if progress:
        progress("开始下载…", 0.0)
    _download_url_to_path(info.url, target, progress=progress)
    setup = target
    if target.suffix.lower() == ".zip":
        if progress:
            progress("正在解压…", None)
        extract = work / "extracted"
        extract.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "r") as zf:
            zf.extractall(extract)
        direct = extract / "Setup.exe"
        if direct.is_file():
            setup = direct
        else:
            hits = sorted(extract.rglob("Setup.exe"))
            if not hits:
                raise FileNotFoundError(f"解压后未找到 Setup.exe: {extract}")
            setup = hits[0]
    if progress:
        progress("启动安装程序…", 1.0)
    if sys.platform.startswith("win"):
        subprocess.Popen([str(setup)], shell=False)  # noqa: S603
    else:
        subprocess.Popen(["xdg-open", str(setup)])  # noqa: S603
    return setup
