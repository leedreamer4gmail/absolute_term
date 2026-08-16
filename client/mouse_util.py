#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 鼠标原语：按下 / 抬起 / 拖动（供淘宝滑块自动拖用）。"""

from __future__ import annotations

import math
import random
import time
from typing import Iterable


def _user32():
    import ctypes

    return ctypes.windll.user32


def mouse_move(x: int, y: int) -> None:
    """光标移到屏幕绝对坐标。"""
    if not _user32().SetCursorPos(int(x), int(y)):
        raise OSError(f"SetCursorPos 失败: ({x}, {y})")


def mouse_down(button: str = "left") -> None:
    """按下鼠标键。button: left|right|middle"""
    import ctypes

    flags = {"left": 0x0002, "right": 0x0008, "middle": 0x0020}
    f = flags.get((button or "left").lower())
    if f is None:
        raise ValueError(f"未知按键: {button}")
    _user32().mouse_event(f, 0, 0, 0, 0)


def mouse_up(button: str = "left") -> None:
    """抬起鼠标键。"""
    import ctypes

    flags = {"left": 0x0004, "right": 0x0010, "middle": 0x0040}
    f = flags.get((button or "left").lower())
    if f is None:
        raise ValueError(f"未知按键: {button}")
    _user32().mouse_event(f, 0, 0, 0, 0)


def mouse_click(x: int, y: int, button: str = "left", hold_ms: float = 40) -> None:
    mouse_move(x, y)
    time.sleep(0.02)
    mouse_down(button)
    time.sleep(max(hold_ms, 1) / 1000.0)
    mouse_up(button)


def _ease_points(x1: int, y1: int, x2: int, y2: int, steps: int) -> list[tuple[int, int]]:
    """带轻微抖动的缓动轨迹。"""
    steps = max(8, int(steps))
    pts: list[tuple[int, int]] = []
    for i in range(steps + 1):
        t = i / steps
        # ease-in-out
        e = 0.5 - 0.5 * math.cos(math.pi * t)
        x = x1 + (x2 - x1) * e
        y = y1 + (y2 - y1) * e
        if 0 < i < steps:
            x += random.uniform(-1.2, 1.2)
            y += random.uniform(-0.8, 0.8)
        pts.append((int(round(x)), int(round(y))))
    return pts


def mouse_drag(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    steps: int = 28,
    duration: float = 0.75,
    button: str = "left",
) -> None:
    """按下 → 沿轨迹拖动 → 抬起。"""
    pts = _ease_points(x1, y1, x2, y2, steps)
    mouse_move(pts[0][0], pts[0][1])
    time.sleep(0.05 + random.uniform(0, 0.05))
    mouse_down(button)
    time.sleep(0.04)
    gap = max(duration, 0.2) / max(len(pts) - 1, 1)
    for x, y in pts[1:]:
        mouse_move(x, y)
        time.sleep(gap * random.uniform(0.85, 1.15))
    time.sleep(0.05)
    mouse_up(button)


def mouse_drag_path(points: Iterable[tuple[int, int]], *, button: str = "left",
                    duration: float = 0.8) -> None:
    pts = [(int(a), int(b)) for a, b in points]
    if len(pts) < 2:
        raise ValueError("拖动路径至少 2 个点")
    mouse_move(pts[0][0], pts[0][1])
    time.sleep(0.04)
    mouse_down(button)
    gap = max(duration, 0.2) / (len(pts) - 1)
    for x, y in pts[1:]:
        mouse_move(x, y)
        time.sleep(gap)
    mouse_up(button)
