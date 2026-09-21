"""HTTP routes."""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from model.dto import web_requests as dto
from service.account.keepalive_service import keepalive_one
from service.web import rescue_adapter
from service.web import runtime as rt

logger = logging.getLogger(__name__)

rescue_and_persist = rescue_adapter.rescue_and_persist
rescue_proxy_raw = rescue_adapter.rescue_proxy_raw
count_rescues_from_log = rescue_adapter.count_rescues_from_log

router = APIRouter()

@router.post("/api/verify-combo")
def verify_combo(req: dto.VerifyComboRequest) -> JSONResponse:
    email = (req.email or "").strip()
    refresh_token = (req.refresh_token or "").strip()
    if req.combo:
        c_email, _pwd, _cid, c_rt = rt._split_combo(req.combo)
        email = email or c_email
        refresh_token = refresh_token or c_rt
    if not refresh_token:
        return JSONResponse({"ok": False, "email": email, "usable": [],
                             "message": "缺少 refresh_token（该 combo 第四段为空，无法校验）。"})
    try:
        res = rt._verify_one(email, refresh_token, _proxy_url(req.proxy), req.test_imap)
        try:
            _cache_verify(res)
        except Exception as exc:  # noqa: BLE001
            logger.exception("缓存测活结果失败: %s", exc)
        return JSONResponse(res)
    except Exception as exc:  # noqa: BLE001
        logger.exception("测活异常 %s: %s", email, exc)
        transient = rt.graph_mail.is_transient_error(exc)
        return JSONResponse({
            "ok": False,
            "transient": transient,
            "unable": transient,
            "email": email,
            "usable": [],
            "summary": (
                "测活暂不可用（网络/SSL），未改账号状态"
                if transient
                else f"测活异常: {exc}"[:160]
            ),
            "message": str(exc)[:160],
        })



@router.post("/api/verify-batch")
def verify_batch(req: dto.VerifyBatchRequest) -> JSONResponse:
    proxy_url = _proxy_url(req.proxy)
    tasks: list[tuple[str, str]] = []  # (email, refresh_token)
    if req.combos:
        for c in req.combos:
            e, _p, _c, refresh_tok = rt._split_combo(c)
            if e:
                tasks.append((e, refresh_tok))
    else:
        rows = rt._load_accounts()
        want = set(req.emails) if req.emails else None
        for r in rows:
            if want is not None and r["email"] not in want:
                continue
            if not r.get("refresh_token"):
                continue
            tasks.append((r["email"], r["refresh_token"]))

    if not tasks:
        return JSONResponse({"ok": True, "results": [], "message": "无可校验账号（缺 refresh_token）。"})

    conc = max(1, min(int(req.concurrency or 4), 8, len(tasks)))
    results: list[dict[str, Any]] = []

    def work(item: tuple[str, str]) -> dict[str, Any]:
        try:
            r = rt._verify_one(item[0], item[1], proxy_url, req.test_imap)
        except Exception as exc:  # noqa: BLE001
            transient = rt.graph_mail.is_transient_error(exc)
            r = {
                "ok": False,
                "transient": transient,
                "unable": transient,
                "email": item[0],
                "usable": [],
                "summary": (
                    "测活暂不可用（网络/SSL），未改账号状态"
                    if transient
                    else f"测活异常: {exc}"[:160]
                ),
                "message": str(exc)[:160],
            }
        if not r.get("transient"):
            _cache_verify(r)
        return r

    with ThreadPoolExecutor(max_workers=conc) as ex:
        for f in as_completed([ex.submit(work, t) for t in tasks]):
            results.append(f.result())

    ok_n = sum(1 for r in results if r.get("ok"))
    return JSONResponse({"ok": True, "total": len(results), "usable": ok_n, "results": results})


# ---------------------------------------------------------------------------
# IMAP / 保活（占位，如实反馈）
# ---------------------------------------------------------------------------



@router.post("/api/imap-enable")
def imap_enable() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "implemented": False,
            "required": False,
            "message": "开启 IMAP 为可选项，非必需：默认走 Graph 令牌读信，不依赖 IMAP 协议开关。"
            "主动开启（SetConsumerMailbox）需网页会话 OWA usertoken，纯 API 链路暂未产出；"
            "且新号会返回 412（反滥用），约 10–24h 账号成熟后才可能开成。"
            "确认某号 IMAP 状态请用『测活』勾选 IMAP。",
        }
    )


