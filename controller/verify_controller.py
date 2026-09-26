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
                             "message": "Missing refresh_token (the 4th segment of combo is empty, cannot verify)."})
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
                "Liveness check temporarily unavailable (Network/SSL), account status unchanged"
                if transient
                else f"Check error: {exc}"[:160]
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
        return JSONResponse({"ok": True, "results": [], "message": "No accounts available for verification (refresh_token required)."})

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
                    "Liveness check temporarily unavailable (Network/SSL), account status unchanged"
                    if transient
                    else f"Check error: {exc}"[:160]
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
            "message": "Enabling IMAP is optional, not required: Graph token is used by default for reading mail, which does not rely on the IMAP protocol switch. "
            "Actively enabling it (SetConsumerMailbox) requires a web session OWA usertoken, which the pure API route does not currently produce; "
            "and new accounts will return 412 (anti-abuse), taking about 10–24h to mature before it can be enabled. "
            "To check the IMAP status of an account, please use 'Liveness Check' and select IMAP.",
        }
    )


