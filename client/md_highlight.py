#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Markdown 源文件着色（参考 livestream mengxiang/ui/qna_editor.highlight_markdown）。

只高亮源码语法，不做 preview 渲染。
"""

from __future__ import annotations

import re
import tkinter as tk

# 暗色主题下的标记色（对齐云端弹窗）
_BLUE = "#9AA3E8"
_ORANGE = "#E09A5A"
_YELLOW = "#E8B84A"
_GREEN = "#34D399"
_MUTED = "#8A8F98"


def highlight_markdown(text_widget: tk.Text) -> None:
    """给 Text 里的 Markdown 源码上色。"""
    text_widget.tag_configure(
        "md_header", foreground=_BLUE, font=("Microsoft YaHei UI", 10, "bold")
    )
    text_widget.tag_configure(
        "md_bold", foreground=_ORANGE, font=("Microsoft YaHei UI", 10, "bold")
    )
    text_widget.tag_configure("md_italic", font=("Microsoft YaHei UI", 10, "italic"))
    text_widget.tag_configure("md_code", foreground=_YELLOW, font=("Consolas", 10))
    text_widget.tag_configure("md_link", foreground=_BLUE, underline=True)
    text_widget.tag_configure(
        "md_bullet", foreground=_GREEN, font=("Microsoft YaHei UI", 10, "bold")
    )
    text_widget.tag_configure(
        "md_quote", foreground=_MUTED, font=("Microsoft YaHei UI", 10, "italic")
    )

    for tag in (
        "md_header",
        "md_bold",
        "md_italic",
        "md_code",
        "md_link",
        "md_bullet",
        "md_quote",
    ):
        text_widget.tag_remove(tag, "1.0", tk.END)

    content = text_widget.get("1.0", "end-1c")
    lines = content.split("\n")
    for line_idx, line in enumerate(lines, start=1):
        if line.startswith("#"):
            text_widget.tag_add("md_header", f"{line_idx}.0", f"{line_idx}.{len(line)}")
            continue
        stripped = line.lstrip()
        if stripped.startswith(("- ", "* ", "+ ")) or (
            stripped and stripped[0].isdigit() and ". " in stripped[:4]
        ):
            indent = len(line) - len(stripped)
            marker_len = (
                2 if stripped.startswith(("- ", "* ", "+ ")) else stripped.find(". ") + 2
            )
            text_widget.tag_add(
                "md_bullet",
                f"{line_idx}.{indent}",
                f"{line_idx}.{indent + marker_len}",
            )
        if stripped.startswith(">"):
            indent = len(line) - len(stripped)
            text_widget.tag_add(
                "md_quote", f"{line_idx}.{indent}", f"{line_idx}.{len(line)}"
            )

    for m in re.finditer(r"```.*?```", content, re.DOTALL):
        start_pos = text_widget.index(f"1.0 + {m.start()} chars")
        end_pos = text_widget.index(f"1.0 + {m.end()} chars")
        text_widget.tag_add("md_code", start_pos, end_pos)
    for m in re.finditer(r"`[^`\n]+`", content):
        start_pos = text_widget.index(f"1.0 + {m.start()} chars")
        end_pos = text_widget.index(f"1.0 + {m.end()} chars")
        text_widget.tag_add("md_code", start_pos, end_pos)
    for m in re.finditer(r"(\*\*|__)(.*?)\1", content):
        start_pos = text_widget.index(f"1.0 + {m.start()} chars")
        end_pos = text_widget.index(f"1.0 + {m.end()} chars")
        text_widget.tag_add("md_bold", start_pos, end_pos)
    for m in re.finditer(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", content):
        start_pos = text_widget.index(f"1.0 + {m.start()} chars")
        end_pos = text_widget.index(f"1.0 + {m.end()} chars")
        text_widget.tag_add("md_italic", start_pos, end_pos)
    for m in re.finditer(r"(?<!_)_(?!_)(.*?)(?<!_)_(?!_)", content):
        start_pos = text_widget.index(f"1.0 + {m.start()} chars")
        end_pos = text_widget.index(f"1.0 + {m.end()} chars")
        text_widget.tag_add("md_italic", start_pos, end_pos)
    for m in re.finditer(r"\[[^\]\n]+\]\([^\)\n]+\)", content):
        start_pos = text_widget.index(f"1.0 + {m.start()} chars")
        end_pos = text_widget.index(f"1.0 + {m.end()} chars")
        text_widget.tag_add("md_link", start_pos, end_pos)
