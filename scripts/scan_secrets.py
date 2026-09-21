#!/usr/bin/env python3
"""扫描仓库中的代理凭据、API Key 等敏感信息。

用法:
  python scripts/scan_secrets.py              # 扫描工作区（含未跟踪文件）
  python scripts/scan_secrets.py --tracked    # 仅 git 已跟踪文件
  python scripts/scan_secrets.py --staged     # 仅暂存区（commit 前）
  python scripts/scan_secrets.py --history    # 扫描 git 历史 blob（慢）

退出码: 0=无高危命中, 1=发现疑似真实凭据
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "px_solver/node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    "htmlcov",
    "assets/screenshots",
    "accounts",
    "logs",
    "data",
    "cookies",
    "captures",
    "google_reg",
}

SKIP_FILES = {
    "scripts/scan_secrets.py",
    "LICENSE",
}

TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".sh",
    ".bash",
    ".zsh",
    ".json",
    ".jsonl",
    ".txt",
    ".md",
    ".env",
    ".example",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".html",
    ".css",
    ".xml",
    ".sql",
}

# 命中后若整行匹配任一 allow 片段 → 视为占位符/文档示例
ALLOW_SUBSTRINGS = (
    "user:pass",
    "user:secret",
    "testuser",
    "testpass",
    "myuser",
    "mypass",
    "gate.example.com",
    "gate.example",
    "example.com",
    "your-domain",
    "your-recovery",
    "your-recovery-host",
    "placeholder",
    "user-session-123",
    "user-session-{sid}",
    "host:port:user:pass",
    "http://user:pass@",
    "REDACTED",
    "xxxx",
    "secret",  # 单独作为密码占位符（配合 user_custom_zone 等）
    "pass-US-{sid}",
    "pass-US-xxxx",
    "Bearer key",
    "M.C5xx",
    "apicn.captcha.run",
    "api.offcaptcha.com",
    "api.ez-captcha.com",
)

# 已知曾泄露、历史中必须清零的指纹
KNOWN_LEAKS = (
    "kevin273945",
    "273945asaf",
    "27394500asaf",
    "kevin2739",
    "8848858-f8632b7f",
    "eaba13a4",
    "gvic1257353",
    "egq3oqbg",
    "cliproxy.io",
    "5iBeGTRh",
    "WYyJMhdO",
    "rolaproxy.com",
)

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "proxy-4part",
        re.compile(
            r"(?P<host>[a-zA-Z0-9][a-zA-Z0-9.-]*\.(?:net|io|com|cn|info|live|org))"
            r":(?P<port>\d{3,5})"
            r":(?P<user>[^:\s\"'\\]+)"
            r":(?P<pass>[^:\s\"'\\]+)"
        ),
    ),
    (
        "proxy-url-auth",
        re.compile(r"https?://(?P<user>[^@\s/\"']+):(?P<pass>[^@\s/\"']+)@(?P<host>[^\s\"']+)"),
    ),
    (
        "api-key-ish",
        re.compile(
            r"(?i)(?:api[_-]?key|bearer|token|secret|password)\s*[=:]\s*['\"]"
            r"(?P<val>[a-zA-Z0-9_\-./+=]{12,})['\"]"
        ),
    ),
]

PROVIDER_HINT = re.compile(
    r"kookeey|ipwo|rapidproxy|cliproxy|brightdata|iproyal|brd\.|superproxy|"
    r"luminati|oxylabs|smartproxy|922proxy|922s5|ipidea|abcproxy|netnut|"
    r"dataimpulse|geonode|webshare|decodo|lunaproxy|pyproxy|711proxy",
    re.I,
)


@dataclass
class Hit:
    path: str
    line: int
    rule: str
    excerpt: str
    severity: str  # leak | suspect | info

    def format(self) -> str:
        return f"[{self.severity.upper():7}] {self.path}:{self.line} ({self.rule}) {self.excerpt}"


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def _should_skip_path(p: Path) -> bool:
    rel = _rel(p)
    if rel in SKIP_FILES:
        return True
    parts = set(p.parts)
    if parts & SKIP_DIRS:
        return True
    name = p.name
    if name.endswith((".pyc", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2")):
        return True
    if name.endswith(".db") or name.endswith(".db-wal") or name.endswith(".db-shm"):
        return True
    if p.suffix and p.suffix not in TEXT_SUFFIXES and name not in (".env", ".env.example"):
        return False
    return False


def _is_allowed(line: str, matched: str) -> bool:
    low = line.lower()
    if any(a.lower() in low for a in ALLOW_SUBSTRINGS):
        return True
    # UI placeholder / help text
    if "placeholder=" in low or "help=" in low:
        return True
    # 注释里纯格式说明
    if matched in ("user", "pass", "secret") and "user:" in low:
        return True
    return False


def _severity(rule: str, line: str, matched: str) -> str:
    if any(k in line for k in KNOWN_LEAKS):
        return "leak"
    if rule.startswith("proxy"):
        if PROVIDER_HINT.search(line):
            return "suspect" if not _is_allowed(line, matched) else "info"
        host = matched.split(":")[0] if ":" in matched else matched
        if "example" in host:
            return "info"
        return "suspect"
    if rule == "api-key-ish":
        if matched in ("",) or len(matched) < 16:
            return "info"
        return "suspect"
    return "suspect"


def scan_text(path: str, text: str) -> list[Hit]:
    hits: list[Hit] = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # 仍扫 .env 赋值行
            if "=" not in stripped:
                continue
        for leak in KNOWN_LEAKS:
            if leak in line:
                hits.append(Hit(path, i, "known-leak", leak, "leak"))
        for rule, pat in PATTERNS:
            for m in pat.finditer(line):
                excerpt = m.group(0)
                if len(excerpt) > 100:
                    excerpt = excerpt[:97] + "..."
                if _is_allowed(line, excerpt):
                    continue
                sev = _severity(rule, line, excerpt)
                hits.append(Hit(path, i, rule, excerpt, sev))
    return hits


def iter_files(mode: str) -> list[Path]:
    if mode == "tracked":
        out = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
        return [ROOT / p for p in out.splitlines() if p]
    if mode == "staged":
        out = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=ROOT, text=True)
        return [ROOT / p for p in out.splitlines() if p]
    files: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or _should_skip_path(p):
            continue
        files.append(p)
    return sorted(files)


def scan_files(mode: str) -> list[Hit]:
    all_hits: list[Hit] = []
    for p in iter_files(mode):
        if _should_skip_path(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        all_hits.extend(scan_text(_rel(p), text))
    return all_hits


def scan_history() -> list[Hit]:
    """仅扫 git 已跟踪文件在各 commit 中的内容。"""
    hits: list[Hit] = []
    try:
        tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    except subprocess.CalledProcessError:
        return hits
    for rel in tracked:
        if not rel or _should_skip_path(ROOT / rel):
            continue
        try:
            content = subprocess.check_output(
                ["git", "log", "-1", "--pretty=format:%H", "--", rel],
                cwd=ROOT,
                text=True,
            )
        except subprocess.CalledProcessError:
            continue
        try:
            blob = subprocess.check_output(
                ["git", "show", f"HEAD:{rel}"],
                cwd=ROOT,
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="ignore")
        except subprocess.CalledProcessError:
            continue
        hits.extend(scan_text(f"HEAD:{rel}", blob))
        # 历史指纹：已知泄露串是否曾出现在该文件历史中
        try:
            for leak in KNOWN_LEAKS:
                out = subprocess.check_output(
                    ["git", "log", "-S", leak, "--oneline", "--", rel],
                    cwd=ROOT,
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                if out:
                    first = out.splitlines()[0]
                    hits.append(Hit(rel, 0, "history-leak", f"{leak} in {first}", "leak"))
        except subprocess.CalledProcessError:
            pass
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描代理/API 等敏感信息")
    ap.add_argument("--tracked", action="store_true", help="仅已跟踪文件")
    ap.add_argument("--staged", action="store_true", help="仅暂存区")
    ap.add_argument("--history", action="store_true", help="扫描 git 历史")
    ap.add_argument("--json", action="store_true", help="JSON 行输出")
    ap.add_argument("--summary", action="store_true", help="按文件汇总，不逐行刷屏")
    args = ap.parse_args()

    mode = "all"
    if args.tracked:
        mode = "tracked"
    elif args.staged:
        mode = "staged"

    hits = scan_files(mode)
    if args.history:
        hits.extend(scan_history())

    # 去重
    uniq: dict[tuple[str, int, str, str], Hit] = {}
    for h in hits:
        uniq[(h.path, h.line, h.rule, h.excerpt)] = h
    hits = sorted(uniq.values(), key=lambda x: ({"leak": 0, "suspect": 1, "info": 2}[x.severity], x.path, x.line))

    leaks = [h for h in hits if h.severity == "leak"]
    suspects = [h for h in hits if h.severity == "suspect"]

    if args.json:
        import json

        for h in hits:
            print(json.dumps(h.__dict__, ensure_ascii=False))
    elif args.summary:
        print(f"scan_secrets: mode={mode} history={args.history}")
        print(f"  leak={len(leaks)} suspect={len(suspects)} total={len(hits)}")
        by_file: dict[str, list[Hit]] = {}
        for h in hits:
            by_file.setdefault(h.path, []).append(h)
        for path in sorted(by_file):
            fh = by_file[path]
            sev = "leak" if any(x.severity == "leak" for x in fh) else "suspect"
            rules = sorted({x.rule for x in fh})
            print(f"[{sev.upper():7}] {path}  hits={len(fh)}  rules={','.join(rules)}")
    else:
        print(f"scan_secrets: mode={mode} history={args.history}")
        print(f"  leak={len(leaks)} suspect={len(suspects)} total={len(hits)}")
        for h in hits:
            print(h.format())

    if leaks or suspects:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
