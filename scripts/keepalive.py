#!/usr/bin/env python3
"""Outlook 四段/六段式账号保活脚本。

做什么（每个账号一次轻量往返，尽量拟人、低频）：
  1) 用 refresh_token 换 access_token（graph scope）
  2) 一次轻量 Graph 调用：GET /me（读 profile）+ 列 1 封信，确认账号在线可读
  3) **保存轮换后的新 refresh_token**（微软刷新常返回新的 refresh_token）——回写账号文件

用法：
  python3 scripts/keepalive.py --file accounts/accounts.txt [--proxy h:p:u:p] \
      [--concurrency 5] [--out accounts/accounts.refreshed.txt] [--inplace]

  --inplace   直接原地重写输入文件（用新 refresh_token 替换旧的；失效号加 # 前缀标记）
  --out FILE  写到新文件（默认 <file>.refreshed）
  --dead FILE 失效号单独另存（默认 <file>.dead）

四段：email----password----client_id----refresh_token
六段：email----password----client_id----graph_refresh----login_client_id----login_refresh
      （六段时保活/回写 graph_refresh；login_refresh 原样保留）
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

socket.setdefaulttimeout(30)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from service.account.graph_mail import (  # noqa: E402
    GRAPH_BASE,
    OUTLOOK_REST_BASE,
    _resource_of,
    refresh_token_for,
)

_print_lock = threading.Lock()


def _proxy_url(raw: str) -> str:
    if not raw:
        return ""
    from service.resource.proxy.proxy_utils import parse_proxy

    cfg = parse_proxy(raw)
    return cfg.url if cfg else ""


def _mask(email: str) -> str:
    name, _, dom = email.partition("@")
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}***@{dom}"


def _is_cid(p: str) -> bool:
    return len(p) == 36 and p.count("-") == 4


def parse_combo(line: str):
    """→ (parts, graph_rt_index) 或 None。graph_rt_index 指向要保活/回写的 refresh_token。"""
    parts = line.split("----")
    if len(parts) < 4:
        return None
    # graph refresh token 一律取第 4 段（index 3）
    return parts, 3


def keepalive_one(line: str, proxy_url: str = "") -> dict:
    line = line.strip()
    if not line or line.startswith("#"):
        return {"skip": True, "line": line}
    parsed = parse_combo(line)
    if not parsed:
        return {"ok": False, "line": line, "detail": "非法格式"}
    parts, rt_idx = parsed
    email = parts[0]
    client_id = parts[2] if _is_cid(parts[2]) else ""
    rt = parts[rt_idx]

    if client_id:
        data = refresh_token_for(rt, "", client_id=client_id, proxy_url=proxy_url)
    else:
        data = refresh_token_for(rt, "", proxy_url=proxy_url)
    at = data.get("access_token", "")
    if not at:
        return {
            "ok": False, "email": email, "line": line,
            "detail": str(data.get("error_description", data.get("error")))[:80],
        }

    # 轻量往返：profile + 1 封信。按令牌实际 scope 路由——
    # 卖家/IMAP scope 令牌走 Outlook REST（Graph 会 401），Graph 令牌走 Graph。
    resource = _resource_of(data.get("scope", ""))
    if resource == "outlook":
        me_url = OUTLOOK_REST_BASE + "/me"
        msg_url = OUTLOOK_REST_BASE + "/me/messages?$top=1&$select=Subject"
    else:  # graph 或 unknown 一律按 Graph 试
        me_url = GRAPH_BASE + "/me?$select=userPrincipalName"
        msg_url = GRAPH_BASE + "/me/messages?$top=1&$select=subject"
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    prof_ok = msg_ok = False
    try:
        r = requests.get(me_url, headers={"Authorization": "Bearer " + at},
                         proxies=proxies, timeout=30)
        prof_ok = r.status_code == 200
        r2 = requests.get(msg_url, headers={"Authorization": "Bearer " + at},
                          proxies=proxies, timeout=30)
        msg_ok = r2.status_code == 200
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "email": email, "line": line, "detail": f"读信异常:{exc}"[:80]}

    new_rt = data.get("refresh_token", "") or rt  # 轮换新 rt；无则沿用
    rotated = new_rt != rt
    new_parts = list(parts)
    new_parts[rt_idx] = new_rt
    return {
        "ok": prof_ok and msg_ok, "email": email,
        "profile": prof_ok, "message": msg_ok, "rotated": rotated,
        "new_line": "----".join(new_parts),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Outlook 账号保活 + refresh_token 轮换回写")
    ap.add_argument("--file", required=True, help="账号文件（四段/六段，每行一个）")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--out", default="", help="保活后新文件（默认 <file>.refreshed）")
    ap.add_argument("--dead", default="", help="失效号另存（默认 <file>.dead）")
    ap.add_argument("--inplace", action="store_true", help="原地重写输入文件")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    proxy_url = _proxy_url(args.proxy)
    src = Path(args.file)
    lines = [l.rstrip("\n") for l in src.read_text(encoding="utf-8").splitlines()]
    if args.limit:
        lines = lines[: args.limit]

    out_path = Path(args.out) if args.out else src.with_suffix(src.suffix + ".refreshed")
    dead_path = Path(args.dead) if args.dead else src.with_suffix(src.suffix + ".dead")

    alive_lines: dict[int, str] = {}
    dead_lines: list[str] = []
    ok = dead = 0

    def _task(i: int, line: str):
        return i, keepalive_one(line, proxy_url)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = [pool.submit(_task, i, l) for i, l in enumerate(lines)]
        for fut in as_completed(futs):
            i, res = fut.result()
            if res.get("skip"):
                alive_lines[i] = res["line"]
                continue
            if res.get("ok"):
                alive_lines[i] = res["new_line"]
            else:
                alive_lines[i] = "# DEAD " + (res.get("line") or "")
                dead_lines.append(res.get("line") or "")
            with _print_lock:
                em = _mask(res.get("email", "?"))
                if res.get("ok"):
                    print(f"[OK] {em} profile={res.get('profile')} msg={res.get('message')} "
                          f"rotated={res.get('rotated')}")
                else:
                    print(f"[DEAD] {em} {res.get('detail','')}")

    # 统计（在主线程重算，避免闭包计数竞争）
    for i in sorted(alive_lines):
        l = alive_lines[i]
        if l.startswith("# DEAD"):
            dead += 1
        elif l and not l.startswith("#"):
            ok += 1

    ordered = [alive_lines[i] for i in sorted(alive_lines)]
    target = src if args.inplace else out_path
    target.write_text("\n".join(ordered) + "\n", encoding="utf-8")
    if dead_lines:
        dead_path.write_text("\n".join(dead_lines) + "\n", encoding="utf-8")

    print(f"\n保活完成: 存活 {ok} / 失效 {dead}，写入 {target}"
          + (f"，失效号另存 {dead_path}" if dead_lines else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
