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

@router.post("/api/rescue")
def rescue_accounts(req: dto.RescueRequest) -> JSONResponse:
    """对选中账号跑 scripts.rescue_login，回写 token / 恢复邮箱并累计 rescue_count。"""
    if not rt.RESCUE_READY or rescue_and_persist is None or rescue_proxy_raw is None:
        return JSONResponse({
            "ok": False,
            "implemented": False,
            "message": "救援脚本 scripts.rescue_login 无法导入，暂不可用。",
            "results": [],
        })
    rows = rt._load_accounts()
    want = set(req.emails) if req.emails else None
    tasks: list[dict[str, Any]] = []
    for r in rows:
        if want is not None and r["email"] not in want:
            continue
        if not (r.get("password") or "").strip():
            continue
        tasks.append(r)
    if not tasks:
        return JSONResponse({
            "ok": True,
            "implemented": True,
            "total": 0,
            "ok_count": 0,
            "results": [],
            "message": "无可救援账号（需有密码）。",
        })

    use_pool, provider_filter = _parse_proxy_selection(req.model_dump())
    req.use_proxy_pool = use_pool
    proxy = rescue_proxy_raw((req.proxy or "").strip())
    if (req.proxy or "").strip():
        rt.proxy_pool.ensure_templates([(req.proxy or "").strip()], provider="web")
    if use_pool:
        stats = rt.proxy_pool.pool_stats(provider=provider_filter)
        if stats.get("enabled", 0) < 1 and not proxy:
            hint = "代理池无可用条目"
            if provider_filter:
                hint += f"（代理商 {provider_filter}）"
            return JSONResponse({
                "ok": False,
                "implemented": True,
                "message": f"{hint}。请先在「代理池」页添加。",
                "results": [],
            })
    conc = max(1, min(int(req.concurrency or 1), 2, len(tasks)))
    results: list[dict[str, Any]] = []

    def work(row: dict[str, Any]) -> dict[str, Any]:
        email = row["email"]
        one_proxy = proxy
        proxy_meta: dict[str, Any] = {}
        if use_pool:
            one_proxy, proxy_meta = rt.proxy_pool.resolve_for_email(
                email, fallback=proxy, provider=provider_filter,
            )
            one_proxy = rescue_proxy_raw(one_proxy or proxy)
        try:
            out = rescue_and_persist(
                email,
                row.get("password") or "",
                one_proxy,
                recovery_email=row.get("recovery_email") or "",
                write=True,
                accounts_dir=rt.ACCOUNTS_DIR,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("救援异常 %s: %s", email, exc)
            out = {"ok": False, "email": email, "reason": f"{type(exc).__name__}: {exc}"}
        out.setdefault("email", email)
        if use_pool and proxy_meta.get("proxy_id"):
            rt.proxy_pool.record_result(
                proxy_meta["proxy_id"],
                success=bool(out.get("ok")),
                reg_country="",
                purpose="rescue",
                email=email,
                error=(out.get("reason") or "") if not out.get("ok") else "",
            )
        if out.get("ok") and out.get("refresh_token"):
            try:
                v = rt._verify_one(email, out["refresh_token"], "", False)
                _cache_verify(v)
                out["verify_ok"] = bool(v.get("ok"))
                out["verify_summary"] = v.get("summary", "")
            except Exception as exc:  # noqa: BLE001
                out["verify_ok"] = False
                out["verify_summary"] = str(exc)[:120]
        slim = {k: v for k, v in out.items() if k != "refresh_token" or not out.get("ok")}
        return slim

    with ThreadPoolExecutor(max_workers=conc) as ex:
        for f in as_completed([ex.submit(work, t) for t in tasks]):
            results.append(f.result())

    ok_n = sum(1 for r in results if r.get("ok"))
    return JSONResponse({
        "ok": True,
        "implemented": True,
        "total": len(results),
        "ok_count": ok_n,
        "results": results,
    })



@router.post("/api/keepalive")
def keepalive(req: dto.KeepaliveRequest) -> JSONResponse:
    """对选中（或全部）账号并发跑 keepalive_one：refresh→access→GET /me+列信→轮换回写。"""
    if not rt.KEEPALIVE_READY or keepalive_one is None:
        return JSONResponse({"ok": False, "implemented": False,
                             "message": "保活脚本 scripts.keepalive 无法导入，暂不可用。"})
    proxy_url = _proxy_url(req.proxy)
    rows = rt._load_accounts()
    want = set(req.emails) if req.emails else None
    tasks: list[tuple[str, str]] = []  # (email, combo_line)
    for r in rows:
        if want is not None and r["email"] not in want:
            continue
        line = r.get("combo_dual") or r.get("combo") or ""
        if not line or not r.get("has_token"):
            continue
        tasks.append((r["email"], line))
    if not tasks:
        return JSONResponse({"ok": True, "implemented": True, "results": [],
                             "message": "无可保活账号（缺 refresh_token）。"})

    conc = max(1, min(int(req.concurrency or 5), 5, len(tasks)))
    results: list[dict[str, Any]] = []

    def work(item: tuple[str, str]) -> dict[str, Any]:
        email, line = item
        try:
            res = keepalive_one(line, proxy_url)
        except Exception as exc:  # noqa: BLE001
            return {"email": email, "ok": False, "detail": f"异常:{exc}"[:120]}
        res.setdefault("email", email)
        return res

    with ThreadPoolExecutor(max_workers=conc) as ex:
        for f in as_completed([ex.submit(work, t) for t in tasks]):
            results.append(f.result())

    # 回写轮换后的新 refresh_token（仅在真的轮换时写盘）
    with rt._save_lock:
        for r in results:
            if r.get("ok") and r.get("rotated") and r.get("new_line"):
                rt._writeback_keepalive(r.get("email", ""), r["new_line"])
    # 缓存保活结果到 meta（作为测活状态）
    for r in results:
        if r.get("skip"):
            continue
        email = r.get("email")
        if not email:
            continue
        now = datetime.now().isoformat()
        rt._update_meta(email, {"verify": {
            "ok": bool(r.get("ok")),
            "usable": ["graph"] if r.get("ok") else [],
            "graph": bool(r.get("profile")),
            "checked_at": now,
            "source": "keepalive",
        }})
        json_patch: dict[str, Any] = {"updated_at": now}
        if r.get("ok"):
            json_patch["last_alive_at"] = now
        _patch_account_json(email, json_patch)

    ok_n = sum(1 for r in results if r.get("ok"))
    rot_n = sum(1 for r in results if r.get("rotated"))
    return JSONResponse({
        "ok": True, "implemented": True, "total": len(results),
        "alive": ok_n, "rotated": rot_n,
        "results": [{
            "email": r.get("email"),
            "ok": bool(r.get("ok")),
            "profile": r.get("profile"),
            "message": r.get("message"),
            "rotated": bool(r.get("rotated")),
            "skip": bool(r.get("skip")),
            "detail": r.get("detail", ""),
        } for r in results],
    })



@router.post("/api/replenish")
def replenish_pool_api(req: dto.ReplenishRequest) -> JSONResponse:
    """回补收码池：把选中/全部账号中 graph 可用的四段式去重追加进 proof pool。"""
    try:
        from service.account.graph_mail import probe_token
        from service.resource.recovery.proof_pool import pool_path
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "implemented": False, "message": f"收码池模块不可用: {exc}"})
    pool = pool_path() or (rt.PROJECT_DIR.parent / "1000outlook.txt")
    pool = Path(pool)
    proxy_url = _proxy_url(req.proxy)
    rows = rt._load_accounts()
    want = set(req.emails) if req.emails else None
    existing: set[str] = set()
    if pool.exists():
        for l in pool.read_text(encoding="utf-8", errors="replace").splitlines():
            l = l.strip()
            if l and not l.startswith("#") and "----" in l:
                existing.add(l.split("----")[0].lower())
    added = dup = bad = 0
    to_write: list[str] = []
    for r in rows:
        if want is not None and r["email"] not in want:
            continue
        parts = (r.get("combo") or "").split("----")
        if len(parts) < 4 or not parts[3]:
            bad += 1
            continue
        email, refresh_tok = parts[0], parts[3]
        four = "----".join(parts[:4])
        if email.lower() in existing:
            dup += 1
            continue
        if req.verify:
            try:
                pr = probe_token(email, refresh_tok, proxy_url=proxy_url)
            except Exception:  # noqa: BLE001
                pr = {"usable": []}
            if not pr.get("usable"):
                bad += 1
                continue
        to_write.append(four)
        existing.add(email.lower())
        added += 1
    if to_write:
        with rt._save_lock:
            pool.parent.mkdir(parents=True, exist_ok=True)
            with pool.open("a", encoding="utf-8") as fp:
                for l in to_write:
                    fp.write(l + "\n")
    return JSONResponse({"ok": True, "implemented": True, "pool": str(pool),
                         "added": added, "duplicate": dup, "skipped": bad})


# ---------------------------------------------------------------------------
# 代理池
# ---------------------------------------------------------------------------


