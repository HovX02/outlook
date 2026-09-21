#!/usr/bin/env python3
"""校验四段式（email----password----client_id----refresh_token）可用性。

自动探测三种读信方式（都基于同一 refresh_token）：
  - graph        : graph.microsoft.com/v1.0/me/messages（新号注册完即用，推荐）
  - outlook_rest : outlook.office.com/api/v2.0/me/messages（同样绕开 IMAP 开关）
  - imap         : 传统 IMAP XOAUTH2（新号常见「authenticated but not connected」= IMAP 未开启）

用法：
  python3 scripts/check_imap.py "email----pwd----client_id----refresh_token" [--proxy h:p:u:p] [--imap]
  python3 scripts/check_imap.py --file 1000outlook.txt [--limit 20]
"""
from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

socket.setdefaulttimeout(25)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.account.enable_imap import imap_login_test  # noqa: E402
from service.account.graph_mail import probe_login_token, probe_token  # noqa: E402


def _proxy_url(raw: str) -> str:
    if not raw:
        return ""
    from service.resource.proxy.proxy_utils import parse_proxy

    cfg = parse_proxy(raw)
    return cfg.url if cfg else ""


def _is_cid(p: str) -> bool:
    return len(p) == 36 and p.count("-") == 4


def _split(combo: str):
    """解析四段或六段 combo。

    四段: email----password----client_id----refresh_token
    六段: email----password----client_id----graph_refresh----login_client_id----login_refresh
    返回 (email, cid, rt, login_cid, login_rt)。
    """
    parts = combo.strip().split("----")
    if len(parts) < 4:
        return None
    email = parts[0]
    cid = next((p for p in parts if _is_cid(p)), "")
    rts = [p for p in parts if p.startswith("M.C")]
    rt = rts[0] if rts else parts[3]
    login_cid = ""
    login_rt = ""
    if len(parts) >= 6:
        login_rt = rts[1] if len(rts) >= 2 else parts[5]
        # login_client_id = 第 5 段（若是 uuid）
        login_cid = parts[4] if _is_cid(parts[4]) else cid
    return email, cid, rt, login_cid, login_rt


def check_one(combo: str, proxy_url: str = "", test_imap: bool = False) -> bool:
    sp = _split(combo)
    if not sp:
        print(f"[SKIP] 非四段式: {combo[:40]}")
        return False
    email, _cid, rt, login_cid, login_rt = sp
    if not rt:
        print(f"[EMPTY] {email} 无 refresh_token")
        return False
    res = probe_token(email, rt, proxy_url=proxy_url)
    usable = res["usable"]
    line = f"[{'OK' if usable else 'FAIL'}] {email} usable={usable or '无'} detail={res['detail']}"
    if login_rt:
        lp = probe_login_token(
            login_rt, client_id=login_cid or _cid, proxy_url=proxy_url,
        )
        line += (
            f" | login_token={'OK' if lp['usable'] else 'FAIL'}"
            f" id_token={lp['id_token']}"
        )
        if lp.get("error"):
            line += f" login_err={lp['error']}"
    if test_imap:
        im = imap_login_test(email, rt, proxy_url=proxy_url, select_inbox=True)
        line += f" imap={'OK' if im.get('ok') else im.get('stage')+':'+str(im.get('detail'))[:40]}"
    print(line)
    return bool(usable)


def main() -> int:
    ap = argparse.ArgumentParser(description="四段式令牌可用性校验 (graph/outlook_rest/imap)")
    ap.add_argument("combo", nargs="?", default="")
    ap.add_argument("--file")
    ap.add_argument("--limit", type=int, default=0, help="文件模式只测前 N 条")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--imap", action="store_true", help="额外做一次 IMAP XOAUTH2 登录探测")
    args = ap.parse_args()

    proxy_url = _proxy_url(args.proxy)
    if args.file:
        ok = tot = 0
        for i, line in enumerate(open(args.file, encoding="utf-8")):
            line = line.strip()
            if not line:
                continue
            if args.limit and i >= args.limit:
                break
            tot += 1
            ok += 1 if check_one(line, proxy_url, args.imap) else 0
        print(f"\n汇总: {ok}/{tot} 可用（graph/outlook_rest 任一即算可用）")
        return 0
    if not args.combo:
        ap.print_help()
        return 1
    return 0 if check_one(args.combo, proxy_url, args.imap) else 1


if __name__ == "__main__":
    sys.exit(main())
