#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OCR HTTP 服务 — 基于 RapidOCR(onnxruntime),供本地扫描器批量调用。

用法: POST /ocr (multipart: file=图片) 或 POST /ocr_url (json: {"url": "..."})
返回: {"texts": ["行1", "行2", ...], "count": N}
"""
from __future__ import annotations

import io
import json
import urllib.request

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from rapidocr_onnxruntime import RapidOCR

app = FastAPI(title="company-ocr")
_ocr: RapidOCR | None = None


def get_ocr() -> RapidOCR:
    global _ocr
    if _ocr is None:
        _ocr = RapidOCR()
    return _ocr


class UrlReq(BaseModel):
    url: str
    headers: dict = {}


@app.get("/health")
def health():
    return {"ok": True}


def _run_ocr(img_bytes: bytes) -> list[str]:
    result, _ = get_ocr()(img_bytes)
    if not result:
        return []
    # result: [[box, text, score], ...]
    return [str(r[1]) for r in result]


@app.post("/ocr")
async def ocr_file(file: UploadFile = File(...)):
    data = await file.read()
    try:
        texts = _run_ocr(data)
        return JSONResponse({"texts": texts, "count": len(texts)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/ocr_url")
def ocr_url(req: UrlReq):
    """服务端直接抓 URL 图片做 OCR(绕过本地下载)。"""
    try:
        hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        hdr.update(req.headers or {})
        r = urllib.request.Request(req.url, headers=hdr)
        with urllib.request.urlopen(r, timeout=60) as resp:
            data = resp.read()
        texts = _run_ocr(data)
        return JSONResponse({"texts": texts, "count": len(texts)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8799)
