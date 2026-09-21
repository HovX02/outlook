#!/usr/bin/env python3
"""把新注册产出的可用四段式回补进「收码池」（proof pool 会枯竭）。

收码池每行四段式：email----password----client_id----refresh_token（Graph 可读）。
本脚本从来源（accounts.txt / accounts_dual.txt / 单条 combo）取账号，**校验 graph=200**
后**去重追加**进池文件（默认 OUTLOOK_PROOF_POOL_FILE 或仓库根 1000outlook.txt）。

用法：
  # 从注册产出的 accounts.txt 回补（只补 graph 可用的）
  python3 scripts/replenish_pool.py --from accounts/accounts.txt [--proxy h:p:u:p]

  # 指定池文件、不校验直接追加（谨慎）
  python3 scripts/replenish_pool.py --from accounts/accounts.txt --pool /path/1000outlook.txt --no-verify

  # 直接补一条
  python3 scripts/replenish_pool.py --combo "email----pwd----cid----M.C..."

六段来源会自动取前四段（graph 部分）入池。
"""
from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

socket.setdefaulttimeout(25)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.account.graph_mail import probe_token  # noqa: E402
from service.resource.recovery.proof_pool import pool_path  # noqa: E402


def _proxy_url(raw: str) -> str:
    if not raw:
        return ""
    from service.resource.proxy.proxy_utils import parse_proxy

    cfg = parse_proxy(raw)
    return cfg.url if cfg else ""


def _first4(line: str) -> str:
    parts = line.strip().split("----")
    if len(parts) < 4:
        return ""
    return "----".join(parts[:4])


def _emails_in_pool(pool: Path) -> set[str]:
    out: set[str] = set()
    if pool.exists():
        for l in pool.read_text(encoding="utf-8", errors="replace").splitlines():
            l = l.strip()
            if l and not l.startswith("#") and "----" in l:
                out.add(l.split("----")[0].lower())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="回补新四段式进收码池（校验 graph 后去重追加）")
    ap.add_argument("--from", dest="src", help="来源账号文件（accounts.txt / accounts_dual.txt）")
    ap.add_argument("--combo", help="直接补一条 combo")
    ap.add_argument("--pool", help="池文件路径（默认 OUTLOOK_PROOF_POOL_FILE 或 1000outlook.txt）")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--no-verify", action="store_true", help="不校验 graph 直接追加")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    pool = Path(args.pool) if args.pool else pool_path()
    if not pool:
        # 没有现成池文件时，默认落到仓库根 1000outlook.txt
        pool = Path(__file__).resolve().parent.parent.parent / "1000outlook.txt"
    proxy_url = _proxy_url(args.proxy)

    candidates: list[str] = []
    if args.combo:
        candidates.append(args.combo.strip())
    if args.src:
        for l in Path(args.src).read_text(encoding="utf-8").splitlines():
            l = l.strip()
            if l and not l.startswith("#"):
                candidates.append(l)
    if args.limit:
        candidates = candidates[: args.limit]
    if not candidates:
        ap.print_help()
        return 1

    existing = _emails_in_pool(pool)
    added = 0
    skipped_dup = 0
    skipped_bad = 0
    to_write: list[str] = []

    for line in candidates:
        four = _first4(line)
        if not four:
            skipped_bad += 1
            continue
        email = four.split("----")[0]
        rt = four.split("----")[3]
        if email.lower() in existing:
            skipped_dup += 1
            continue
        if not args.no_verify:
            res = probe_token(email, rt, proxy_url=proxy_url)
            if not res.get("usable"):
                print(f"[SKIP-BAD] {email} 不可用 detail={res.get('detail')}")
                skipped_bad += 1
                continue
        to_write.append(four)
        existing.add(email.lower())
        added += 1
        print(f"[ADD] {email}")

    if to_write:
        with pool.open("a", encoding="utf-8") as fp:
            for l in to_write:
                fp.write(l + "\n")

    print(f"\n回补完成: 新增 {added}，重复跳过 {skipped_dup}，不可用/非法跳过 {skipped_bad}，池文件 {pool}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
