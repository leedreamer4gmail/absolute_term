#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本项目 LLM 配置：读取项目根目录 config.ini 的 [llm]。"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ROOT_CONFIG = PROJECT_ROOT / "config.ini"


def _read_root() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    if not ROOT_CONFIG.is_file():
        raise FileNotFoundError(f"缺少配置: {ROOT_CONFIG}")
    cfg.read(str(ROOT_CONFIG), encoding="utf-8")
    return cfg


def normalize_chat_url(api_url: str) -> str:
    u = (api_url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith("/v1"):
        return u + "/chat/completions"
    if u.endswith("/chat/completions"):
        return u
    return u


def _section_to_conf(section: configparser.SectionProxy) -> dict[str, str]:
    return {
        "api_key": (section.get("api_key") or "").strip(),
        "api_url": normalize_chat_url(section.get("api_url") or ""),
        "model": (section.get("model") or "").strip(),
    }


def load_llm_config(
    require_complete: bool = True,
    config_path: str | Path | None = None,
) -> dict[str, str]:
    """读取 LLM 配置。优先 config_path，否则项目根 config.ini。"""
    path = Path(config_path) if config_path else ROOT_CONFIG
    conf = {"api_key": "", "api_url": "", "model": ""}
    if path.is_file():
        local = configparser.ConfigParser()
        local.optionxform = str
        local.read(str(path), encoding="utf-8")
        if "llm" in local:
            conf = _section_to_conf(local["llm"])
    if require_complete and (not conf["api_key"] or not conf["api_url"] or not conf["model"]):
        raise ValueError("config.ini [llm] 需包含 api_url、model、api_key")
    return conf


def public_llm_config() -> dict[str, str]:
    conf = load_llm_config(require_complete=False)
    key = conf["api_key"]
    masked = "" if not key else (key if len(key) <= 8 else (key[:4] + "…" + key[-4:]))
    return {
        "api_url": conf["api_url"],
        "model": conf["model"],
        "api_key_masked": masked,
        "has_key": bool(key),
    }


def write_llm_config(api_url: str, model: str, api_key: str | None = None) -> dict[str, str]:
    api_url = (api_url or "").strip()
    model = (model or "").strip()
    if not api_url or not model:
        raise ValueError("api_url 与 model 不能为空")
    old = load_llm_config(require_complete=False)
    key = (api_key or "").strip() or old["api_key"]
    if not key:
        raise ValueError("api_key 不能为空")
    text = ROOT_CONFIG.read_text(encoding="utf-8") if ROOT_CONFIG.is_file() else ""
    if "[llm]" not in text:
        text = text.rstrip() + (
            "\n\n[llm]\n"
            f"api_url = {api_url}\n"
            f"model = {model}\n"
            f"api_key = {key}\n"
        )
    else:
        text = re.sub(r"(?m)^api_url\s*=\s*.*$", f"api_url = {api_url}", text, count=1)
        text = re.sub(r"(?m)^model\s*=\s*.*$", f"model = {model}", text, count=1)
        text = re.sub(r"(?m)^api_key\s*=\s*.*$", f"api_key = {key}", text, count=1)
    ROOT_CONFIG.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return public_llm_config()


def call_chat(
    messages: list[dict],
    temperature: float = 0.5,
    timeout: int = 90,
    config_path: str | Path | None = None,
    conf: dict | None = None,
) -> str:
    text, _usage = call_chat_with_usage(
        messages,
        temperature=temperature,
        timeout=timeout,
        config_path=config_path,
        conf=conf,
    )
    return text


def call_chat_with_usage(
    messages: list[dict],
    temperature: float = 0.5,
    timeout: int = 90,
    config_path: str | Path | None = None,
    conf: dict | None = None,
) -> tuple[str, dict]:
    import json
    import urllib.error
    import urllib.request

    if conf is not None:
        use = {
            "api_key": str(conf.get("api_key") or "").strip(),
            "api_url": normalize_chat_url(str(conf.get("api_url") or "")),
            "model": str(conf.get("model") or "").strip(),
        }
        if not use["api_key"] or not use["api_url"] or not use["model"]:
            raise ValueError("内存 LLM 配置不完整：需要 api_url、model、api_key")
    else:
        use = load_llm_config(config_path=config_path)
    payload = {
        "model": use["model"],
        "messages": messages,
        "temperature": temperature,
        "thinking": {"type": "disabled"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        use["api_url"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {use['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM HTTP {e.code}: {detail[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM 网络错误: {e.reason}") from e

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM 返回格式异常: {str(data)[:500]}") from e
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM 返回空内容")
    usage = data.get("usage") or {}
    return content.strip(), {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
