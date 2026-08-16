# -*- mode: python ; coding: utf-8 -*-
"""absolute_term 本机客户端 PyInstaller onedir（参考 livestream packaging，无 TTS）。"""
from pathlib import Path

import rapidocr_onnxruntime
from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None
# SPECPATH = packaging/ ；上级为仓库根
ROOT = Path(SPECPATH).resolve().parent
CLIENT = ROOT / "client"
# RapidOCR 的 onnx 不在 wheel RECORD 里时 collect_data_files 会漏；必须按目录硬拷
OCR_PKG = Path(rapidocr_onnxruntime.__file__).resolve().parent
OCR_MODELS = OCR_PKG / "models"
_onnx = list(OCR_MODELS.glob("*.onnx")) if OCR_MODELS.is_dir() else []
if len(_onnx) < 3:
    raise SystemExit(f"RapidOCR 模型不足（需要 3 个 .onnx）: {OCR_MODELS} -> {_onnx}")
ocr_datas = [
    (str(OCR_PKG / "config.yaml"), "rapidocr_onnxruntime"),
    (str(OCR_MODELS), "rapidocr_onnxruntime/models"),
] + collect_data_files("rapidocr_onnxruntime")
cv2_datas, cv2_bins, cv2_hidden = collect_all("cv2")

a = Analysis(
    [str(CLIENT / "app.py")],
    pathex=[str(CLIENT), str(ROOT)],
    binaries=cv2_bins,
    datas=[
        (str(ROOT / "file" / "img" / "logo.jpg"), "file/img"),
        (str(ROOT / "file" / "img" / "logox.jpg"), "file/img"),
        (str(ROOT / "file" / "img" / "icon_md.png"), "file/img"),
        (str(ROOT / "file" / "img" / "icon_xlsx.png"), "file/img"),
        (str(ROOT / "file" / "img" / "icon_open.png"), "file/img"),
        (str(ROOT / "file" / "absolute_words.md"), "file"),
        (str(ROOT / "file" / "wrong_word.md"), "file"),
        (str(ROOT / "config.ini"), "."),
        (str(CLIENT / "config.example.ini"), "client"),
        # 不打包 .py 源码（version 已编译进 exe）
    ] + ocr_datas + cv2_datas,
    hiddenimports=[
        "shop_store",
        "shop_pipeline",
        "local_scan",
        "chrome_fetch",
        "mouse_util",
        "md_highlight",
        "app_update",
        "ui_icons",
        "ui_layout",
        "link_queue",
        "link_harvest",
        "version",
        "scanner",
        "llm_config",
        "fetch_item",
        "fetch_shop",
        "cookie_util",
        "PIL",
        "PIL.Image",
        "PIL.ImageTk",
        "openpyxl",
        "rapidocr_onnxruntime",
        "onnxruntime",
        "cv2",
        "pyclipper",
        "shapely",
        "shapely.geometry",
        "playwright",
        "playwright.sync_api",
        "browser_cookie3",
        "requests",
        "certifi",
        "bs4",
    ] + list(cv2_hidden),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch", "torchvision", "torchaudio", "tensorflow", "tensorboard",
        "matplotlib", "IPython", "notebook", "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="极限词扫描",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "file" / "img" / "logox.jpg") if (ROOT / "file" / "img" / "logox.jpg").is_file() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="极限词扫描",
)
