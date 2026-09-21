#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."
export PX_SOLVER=swiftshader
export PX_SWIFTSHADER_HEADFUL=1
export PX_OS_PRESS=1
export PX_SWIFTSHADER_TARGET=parent
export OUTLOOK_SKIP_PROOFS=1
export REG_PROXY_RETRIES=1

LOG=/tmp/outlook_reg_batch.log
SUCCESS=/tmp/outlook_reg_success.json
MAX="${1:-25}"

pick_proxy() {
  python3 <<'PY'
import random, re
from service.resource.proxy.proxy_utils import (
    rewrite_ipwo_zone_country, random_sid, preflight_proxy, probe_exit_stability,
)
lines = [l.strip() for l in open("proxies_ipwo.txt") if l.strip() and not l.startswith("#")]
random.shuffle(lines)
for base in lines:
    p = re.sub(r"sid_\d+", f"sid_{random_sid(8)}", base)
    p = rewrite_ipwo_zone_country(p, "US")
    ok, _ = preflight_proxy(p)
    if not ok:
        continue
    sticky, _, _ = probe_exit_stability(p, samples=2)
    if sticky:
        print(p)
        break
PY
}

for i in $(seq 1 "$MAX"); do
  PROXY="$(pick_proxy || true)"
  if [[ -z "${PROXY:-}" ]]; then
    echo "attempt $i: no proxy" | tee -a "$LOG"
    sleep 5
    continue
  fi
  echo "=== BATCH attempt $i $(date) sid=${PROXY##*sid_} ===" | tee -a "$LOG"
  if python3 main.py --country US --proxy "$PROXY" --px-mode local -v >>"$LOG" 2>&1; then
    python3 - "$i" "$PROXY" <<'PY'
import json, sys
from pathlib import Path
acc = sorted(Path("accounts").glob("*.json"), key=lambda p: p.stat().st_mtime)[-1]
data = json.loads(acc.read_text())
payload = {
    "email": data.get("email"),
    "password": data.get("password"),
    "refresh_token": (data.get("refresh_token") or "")[:80],
    "account_file": str(acc),
    "attempt": int(sys.argv[1]),
    "proxy": sys.argv[2],
}
Path("/tmp/outlook_reg_success.json").write_text(json.dumps(payload, indent=2))
print("SUCCESS", payload["email"])
PY
    exit 0
  fi
  echo "attempt $i failed $(date)" | tee -a "$LOG"
  sleep $((3 + RANDOM % 6))
done
exit 1
