#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tk Entry / Text 右键菜单：剪切、复制、粘贴、删除、全选。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any


def _is_entry(w: Any) -> bool:
    return isinstance(w, (tk.Entry, ttk.Entry))


def _is_text(w: Any) -> bool:
    return isinstance(w, tk.Text)


def _has_selection_entry(w: Any) -> bool:
    try:
        return bool(w.selection_present())
    except tk.TclError:
        return False


def _has_selection_text(w: Any) -> bool:
    try:
        w.index("sel.first")
        return True
    except tk.TclError:
        return False


def _clipboard_set(w: Any, text: str) -> None:
    w.clipboard_clear()
    w.clipboard_append(text)
    try:
        w.update()
    except tk.TclError:
        pass


def _cut(w: Any) -> None:
    if _is_entry(w):
        if not _has_selection_entry(w):
            return
        try:
            w.event_generate("<<Cut>>")
        except tk.TclError:
            pass
        return
    if not _is_text(w) or not _has_selection_text(w):
        return
    try:
        text = w.get("sel.first", "sel.last")
        _clipboard_set(w, text)
        was = str(w.cget("state"))
        w.configure(state=tk.NORMAL)
        w.delete("sel.first", "sel.last")
        if was == str(tk.DISABLED):
            w.configure(state=tk.DISABLED)
    except tk.TclError:
        pass


def _copy(w: Any) -> None:
    if _is_entry(w):
        if not _has_selection_entry(w):
            return
        try:
            w.event_generate("<<Copy>>")
        except tk.TclError:
            pass
        return
    if not _is_text(w) or not _has_selection_text(w):
        return
    try:
        _clipboard_set(w, w.get("sel.first", "sel.last"))
    except tk.TclError:
        pass


def _paste(w: Any) -> None:
    if _is_entry(w):
        try:
            w.event_generate("<<Paste>>")
        except tk.TclError:
            pass
        return
    if not _is_text(w):
        return
    try:
        data = w.clipboard_get()
    except tk.TclError:
        return
    if data is None:
        return
    text = str(data)
    try:
        was = str(w.cget("state"))
        if was == str(tk.DISABLED):
            return
        w.configure(state=tk.NORMAL)
        try:
            w.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        w.insert(tk.INSERT, text)
    except tk.TclError:
        pass


def _delete_sel(w: Any) -> None:
    if _is_entry(w):
        if not _has_selection_entry(w):
            return
        try:
            w.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            pass
        return
    if not _is_text(w) or not _has_selection_text(w):
        return
    try:
        was = str(w.cget("state"))
        if was == str(tk.DISABLED):
            return
        w.delete("sel.first", "sel.last")
    except tk.TclError:
        pass


def _select_all(w: Any) -> None:
    if _is_entry(w):
        try:
            w.select_range(0, tk.END)
            w.icursor(tk.END)
        except tk.TclError:
            pass
        return
    if not _is_text(w):
        return
    try:
        was = str(w.cget("state"))
        w.configure(state=tk.NORMAL)
        w.tag_add(tk.SEL, "1.0", "end-1c")
        w.mark_set(tk.INSERT, "1.0")
        w.see(tk.INSERT)
        if was == str(tk.DISABLED):
            w.configure(state=tk.DISABLED)
    except tk.TclError:
        pass


def bind_text_context_menu(
    widget: Any,
    *,
    readonly: bool | None = None,
    bg: str = "#222a34",
    fg: str = "#e8eef4",
    activebackground: str = "#2a3544",
) -> None:
    """给 Entry/Text 绑右键菜单；readonly=None 时按 widget state 判断。"""

    def _readonly() -> bool:
        if readonly is not None:
            return bool(readonly)
        try:
            return str(widget.cget("state")) in (str(tk.DISABLED), "readonly")
        except tk.TclError:
            return False

    menu = tk.Menu(
        widget,
        tearoff=0,
        bg=bg,
        fg=fg,
        activebackground=activebackground,
        activeforeground=fg,
    )

    def _popup(event) -> str | None:
        ro = _readonly()
        menu.delete(0, tk.END)
        if not ro:
            menu.add_command(label="剪切", command=lambda: _cut(widget))
        menu.add_command(label="复制", command=lambda: _copy(widget))
        if not ro:
            menu.add_command(label="粘贴", command=lambda: _paste(widget))
            menu.add_command(label="删除", command=lambda: _delete_sel(widget))
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: _select_all(widget))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    widget.bind("<Button-3>", _popup, add="+")
    # 部分键鼠把右键映射为 Button-2
    widget.bind("<Button-2>", _popup, add="+")

    def _ctrl_a(_event=None) -> str | None:
        _select_all(widget)
        return "break"

    widget.bind("<Control-a>", _ctrl_a, add="+")
    widget.bind("<Control-A>", _ctrl_a, add="+")
