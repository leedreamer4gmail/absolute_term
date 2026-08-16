#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""店铺行图标按钮 + 悬停说明。"""

from __future__ import annotations

from pathlib import Path

from shop_store import bundle_dir


def icon_path(name: str) -> Path:
    p = bundle_dir() / "file" / "img" / name
    if not p.is_file():
        raise FileNotFoundError(f"缺少图标 {p}")
    return p


def load_action_photos(loader) -> dict:
    """loader(path) -> tk PhotoImage。调用方必须保住返回值不被 GC。"""
    return {
        "md": loader(icon_path("icon_md.png")),
        "xlsx": loader(icon_path("icon_xlsx.png")),
        "open": loader(icon_path("icon_open.png")),
    }


class HoverTip:
    def __init__(self, widget, text: str, *, bg: str, fg: str) -> None:
        self.widget = widget
        self.text = text
        self.bg = bg
        self.fg = fg
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _e=None) -> None:
        if self.tip is not None:
            return
        import tkinter as tk

        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw, text=self.text, bg=self.bg, fg=self.fg,
            relief=tk.SOLID, bd=1, padx=6, pady=2,
            font=("Microsoft YaHei UI", 9),
        ).pack()

    def _hide(self, _e=None) -> None:
        if self.tip is None:
            return
        try:
            self.tip.destroy()
        except Exception:  # noqa: BLE001
            pass
        self.tip = None
