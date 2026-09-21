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

@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(content=(rt.STATIC_DIR / "index.html").read_text(encoding="utf-8"))



@router.get("/api/health")
def health() -> JSONResponse:
    db_st = rt.app_db.db_status()
    return JSONResponse(
        {
            "ok": True,
            "accounts_dir": str(rt.ACCOUNTS_DIR),
            "database": db_st,
            "batch_ready": rt.BATCH_READY,
            "dual_ready": rt.DUAL_READY,
            "keepalive_ready": rt.KEEPALIVE_READY,
            "rescue_ready": rt.RESCUE_READY,
        }
    )


