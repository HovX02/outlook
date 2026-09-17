#!/usr/bin/env python3
"""纯协议 press PoC 骨架（仅 requests，无浏览器、无 captcha.run）。

目标：bundle POST → ob 含 fresh _px3 → verify#2 continue。
当前状态：Phase 0 基础设施 + HAR/challenge 解码；Phase 2–3（PoW/编码器）待实现。

用法：
  python px_solver/px_protocol_poc.py decode-har --har outlook.har
  python px_solver/px_protocol_poc.py probe-bundle --uuid ... --vid ... --payload-file ...
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid as uuid_mod
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "px_solver"))

from px_ob_decode import decode_ob, decode_payload_form, tag_xor_key  # noqa: E402

APP_ID = "PXzC5j78di"
TAG = "YjIYfyxJHRR9"
COLLECTOR = f"https://collector-{APP_ID.lower()}.hsprotect.net"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Origin": "https://iframe.hsprotect.net",
        "Referer": "https://iframe.hsprotect.net/",
        "Accept": "*/*",
    })
    return s


def probe_empty_bundle(session: requests.Session, *, vid: str, challenge_uuid: str) -> dict[str, Any]:
    """空 payload 握手：验证 uuid/vid 绑定（预期 do=[]，fresh bake 不会出现）。"""
    data = {
        "appId": APP_ID,
        "tag": TAG,
        "uuid": challenge_uuid,
        "vid": vid,
        "sid": str(uuid_mod.uuid4()),
        "seq": "0",
        "rsc": "1",
        "en": "NTA",
        "payload": "",
    }
    r = session.post(
        f"{COLLECTOR}/assets/js/bundle",
        data=urlencode(data),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    out: dict[str, Any] = {"status": r.status_code, "body_preview": r.text[:200]}
    try:
        j = r.json()
        out["do"] = j.get("do")
        ob = j.get("ob") or ""
        if ob:
            out["ob_segments"] = len(decode_ob(ob, TAG))
    except Exception as exc:  # noqa: BLE001
        out["parse_error"] = str(exc)
    return out


def decode_har_bundles(har_path: Path) -> list[dict[str, Any]]:
    """从 HAR 提取 bundle POST，尝试 decode payload + ob。"""
    har = json.loads(har_path.read_text(encoding="utf-8", errors="replace"))
    rows: list[dict[str, Any]] = []
    for i, e in enumerate((har.get("log") or {}).get("entries") or []):
        req = e.get("request") or {}
        url = req.get("url") or ""
        if req.get("method") != "POST" or "/assets/js/bundle" not in url:
            continue
        post = req.get("postData") or {}
        params: dict[str, str] = {}
        if post.get("params"):
            for p in post["params"]:
                params[p.get("name", "")] = p.get("value") or ""
        elif post.get("text"):
            from urllib.parse import parse_qs
            qs = parse_qs(post["text"], keep_blank_values=True)
            params = {k: (v[0] if v else "") for k, v in qs.items()}
        tag = params.get("tag") or TAG
        plen = len(params.get("payload") or "")
        row: dict[str, Any] = {
            "har_index": i,
            "url": url,
            "payload_len": plen,
            "uuid": (params.get("uuid") or "")[:36],
            "vid": (params.get("vid") or "")[:36],
            "seq": params.get("seq"),
            "pc": (params.get("pc") or "")[:20],
        }
        decoded = decode_payload_form(params)
        if decoded.get("ok"):
            row["payload_json_preview"] = decoded.get("preview", "")[:240]
            row["sts"] = decoded.get("sts")
        else:
            row["payload_decode_error"] = decoded.get("error")
        resp = ((e.get("response") or {}).get("content") or {}).get("text") or ""
        try:
            ob = json.loads(resp).get("ob") or ""
            if ob:
                segs = decode_ob(ob, tag)
                row["ob_seg_count"] = len(segs)
                row["ob_px3"] = any("_px3" in s for s in segs)
                row["ob_pow"] = any("oIIooIoo" in s for s in segs)
        except Exception as exc:  # noqa: BLE001
            row["ob_error"] = str(exc)
        rows.append(row)
    return rows


def solve_press_pure_http(*_args: Any, **_kwargs: Any) -> dict[str, str]:
    """Phase 4 占位：PoW + payload 编码器完成后在此 POST bundle。"""
    raise NotImplementedError(
        "纯协议 press 编码器未实现。"
        "需完成：WASM PoW 提取(px_wasm_extract.py) + payload 编码(unobpx 逆)。"
        "见 px_hold_research.md"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="PX press 纯协议 PoC")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_har = sub.add_parser("decode-har", help="解码 HAR 中 bundle payload/ob")
    p_har.add_argument("--har", type=Path, required=True)

    p_probe = sub.add_parser("probe-bundle", help="活体空 bundle 探针")
    p_probe.add_argument("--uuid", required=True)
    p_probe.add_argument("--vid", required=True)

    args = ap.parse_args()
    if args.cmd == "decode-har":
        rows = decode_har_bundles(args.har)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"\nbundle entries: {len(rows)}")
        return 0
    if args.cmd == "probe-bundle":
        r = probe_empty_bundle(_session(), vid=args.vid, challenge_uuid=args.uuid)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
