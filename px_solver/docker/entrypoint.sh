#!/bin/sh
set -e
# 启动虚拟显示器
Xvfb :99 -screen 0 1920x1080x24 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
i=0
while [ $i -lt 20 ]; do
  if xdpyinfo -display :99 >/dev/null 2>&1; then break; fi
  sleep 0.3; i=$((i+1))
done
echo "[entrypoint] Xvfb :99 ready"
if [ "${1:-}" = "webapp" ]; then
  shift
  exec uvicorn webapp.server:app --host 0.0.0.0 --port "${PORT:-8890}" "$@"
fi
SCRIPT="${1:-px_hold_test.py}"
if [ "$#" -gt 0 ]; then shift; fi
exec python "$SCRIPT" "$@"
