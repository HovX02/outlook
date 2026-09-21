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

@router.get("/api/accounts")
def list_accounts() -> JSONResponse:
    rows = rt._load_accounts()
    return JSONResponse({"count": len(rows), "accounts": rows, "stats": _compute_stats(rows)})



@router.get("/api/accounts/export")
def export_accounts_all() -> PlainTextResponse:
    rows = rt._load_accounts()
    body = "\n".join(r["combo"] for r in rows if r.get("combo"))
    body += "\n" if body else ""
    return PlainTextResponse(body, headers={"Content-Disposition": "attachment; filename=accounts_combo.txt"})



@router.post("/api/accounts/export")
def export_accounts(req: dto.ExportRequest) -> PlainTextResponse:
    fmt = req.format if req.format in rt.EXPORT_FORMATS else "graph"
    rows = rt._load_accounts()
    if req.emails:
        want = set(req.emails)
        rows = [r for r in rows if r["email"] in want]
    lines: list[str] = []
    six_count = 0  # 实际输出为 6 段的行数（dual/recovery 且有对应字段）
    for r in rows:
        c = rt._format_combo(r, fmt)
        if not c:
            continue
        lines.append(c)
        if fmt == "dual" and r.get("combo_dual"):
            six_count += 1
        if fmt == "recovery" and len(c.split("----")) >= 6:
            six_count += 1
    body = "\n".join(lines) + ("\n" if lines else "")
    total = len(lines)
    degraded_full = fmt in ("dual", "recovery") and total > 0 and six_count == 0
    return PlainTextResponse(
        body,
        headers={
            "Content-Disposition": f"attachment; filename=accounts_{fmt}.txt",
            "X-Export-Degraded": "1" if degraded_full else "0",
            "X-Export-Six": str(six_count),
            "X-Export-Total": str(total),
            "Access-Control-Expose-Headers": "X-Export-Degraded,X-Export-Six,X-Export-Total",
        },
    )



@router.post("/api/accounts/import")
def import_accounts(req: dto.ImportRequest) -> JSONResponse:
    """自动识别 4 段/6 段：均写入 accounts.txt（graph 四段）；6 段额外写 accounts_dual.txt
    并把登录令牌存进 meta，便于之后 6 段导出。按邮箱去重、校验字段。"""
    existing = {r["email"] for r in rt._load_accounts()}
    imported = duplicate = invalid = six_seg = 0
    seen: set[str] = set()
    graph_lines: list[str] = []
    dual_lines: list[str] = []
    dual_meta: dict[str, dict[str, str]] = {}
    for raw in (req.text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) < 4:
            invalid += 1
            continue
        email = parts[0].strip()
        if not email or "@" not in email:
            invalid += 1
            continue
        if email in existing or email in seen:
            duplicate += 1
            continue
        seen.add(email)
        graph_lines.append("----".join(parts[:4]))
        imported += 1
        # 6 段：email----pwd----graph_cid----graph_rt----login_cid----login_rt
        if len(parts) >= 6 and parts[4].strip() and parts[5].strip():
            six = "----".join(parts[:6])
            dual_lines.append(six)
            dual_meta[email] = {
                "combo_dual": six,
                "login_client_id": parts[4].strip(),
                "login_refresh_token": parts[5].strip(),
            }
            six_seg += 1
    if graph_lines or dual_lines:
        now = datetime.now().isoformat()
        with rt._save_lock:
            conn = rt.app_db.connect()
            try:
                for ln in graph_lines:
                    parts = ln.split("----")
                    if len(parts) < 4:
                        continue
                    email = parts[0].strip()
                    dual = dual_meta.get(email, {})
                    rt.account_store.upsert_account_dict(conn, {
                        "email": email,
                        "password": parts[1],
                        "client_id": parts[2],
                        "refresh_token": parts[3],
                        "combo": ln,
                        "combo_dual": dual.get("combo_dual", ""),
                        "login_client_id": dual.get("login_client_id", ""),
                        "login_refresh_token": dual.get("login_refresh_token", ""),
                        "success": True,
                        "created_at": now,
                        "updated_at": now,
                        "batch_id": "import",
                        "batch_label": "导入",
                        "legacy_source": "import",
                    })
                conn.commit()
            finally:
                conn.close()
    for email, patch in dual_meta.items():
        rt._update_meta(email, patch)
    return JSONResponse(
        {"ok": True, "imported": imported, "duplicate": duplicate,
         "invalid": invalid, "six_seg": six_seg}
    )



@router.post("/api/accounts/delete")
def delete_accounts(req: dto.DeleteRequest) -> JSONResponse:
    targets = [e for e in req.emails if e]
    if not targets:
        raise HTTPException(status_code=400, detail="未指定要删除的账号。")
    removed = rt.account_store.delete_accounts(targets)
    return JSONResponse({"ok": True, "removed": removed})



@router.post("/api/accounts/meta")
def set_meta(req: dto.MetaRequest) -> JSONResponse:
    patch: dict[str, Any] = {}
    if req.note is not None:
        patch["note"] = req.note
    if req.tags is not None:
        patch["tags"] = req.tags
    if not patch:
        raise HTTPException(status_code=400, detail="无可更新字段。")
    rt._update_meta(req.email, patch)
    return JSONResponse({"ok": True, "email": req.email, **patch})


# ---------------------------------------------------------------------------
# 可用性校验（单条 + 批量）
# ---------------------------------------------------------------------------


