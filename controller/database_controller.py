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

@router.get("/api/database")
def database_status() -> JSONResponse:
    return JSONResponse({"ok": True, **rt.app_db.db_status()})



@router.post("/api/database/backup")
def database_backup() -> JSONResponse:
    try:
        dest = rt.app_db.backup_database(tag="manual")
        return JSONResponse({"ok": True, "path": str(dest), "status": rt.app_db.db_status()})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc



@router.post("/api/database/migrate")
def database_migrate() -> JSONResponse:
    """手动触发遗留 JSON 迁移（通常启动时已自动执行）。"""
    stats = rt.app_db.migrate_legacy_files(rt.ACCOUNTS_DIR)
    return JSONResponse({"ok": True, "migrated": stats, "status": rt.app_db.db_status()})
