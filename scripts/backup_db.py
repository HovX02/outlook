#!/usr/bin/env python3
"""备份 outlook SQLite 数据库到 accounts/backups/。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dao.outlook_dao import backup_database, db_path, db_status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 accounts/outlook.db")
    parser.add_argument("--tag", default="cli", help="备份文件名标签")
    args = parser.parse_args()
    if not db_path().exists():
        print(f"数据库不存在: {db_path()}", file=sys.stderr)
        return 1
    dest = backup_database(tag=args.tag)
    print(f"已备份 -> {dest}")
    st = db_status()
    print(f"账号 {st.get('accounts', 0)} · 代理 {st.get('proxies', 0)} · 最近备份 {st.get('last_backup_at')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
