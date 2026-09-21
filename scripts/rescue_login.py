#!/usr/bin/env python3
"""已有 Outlook 账密 → 重新走 OAuth 拿 Graph refresh_token（不重新注册）。

复用 post_register 的 consent/KMSI/自动表单/换 token，补上 MSA 密码登录。
协议核心（register / CreateAccount / PX）不改。
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from outlook_api_reg.api import risk_initialize, risk_verify
from outlook_api_reg.bootstrap import preload_px_challenge_assets
from outlook_api_reg.constants import GRAPH_MAIL_SCOPE, MAIL_CLIENT_ID, MAIL_REDIRECT_URI
from outlook_api_reg.graph_mail import probe_token
from outlook_api_reg.http_session import OutlookHttpSession
from outlook_api_reg.models import SignupSession
from outlook_api_reg.post_register import (
    _config_str,
    exchange_code_for_token,
    fetch_mail_oauth_code,
    follow_auto_post_forms,
)
from outlook_api_reg.proxy_utils import expand_proxy_template, parse_proxy, preflight_proxy
from outlook_api_reg.px_collector import load_challenge_iframe, post_px_beacon, post_px_bundle, warmup_px_session
from outlook_api_reg.px_cookies import build_challenge_solution, build_px_metadata
from outlook_api_reg.risk import _acquire_silent_px, _solve_px_protocol, load_human_sensor

logger = logging.getLogger("rescue_login")

ACCOUNTS_DIR = _ROOT / "accounts"

_ERR = {
    "80041012": "密码错误",
    "80041013": "密码错误",
    "80043431": "账号不存在",
    "80046704": "帐户或密码不正确",
    "80047803": "账号或密码错误",
    "80048163": "账号被锁/异常",
    "80041170": "需要额外验证",
    "80041032": "会话/表单状态坏了(空PPFT或误重提)",
}


def _ppft(body: str) -> str:
    """登录页常见只有 sFTTag（hidden input），没有独立 sFT 字段。"""
    ft = _config_str(body, "sFT")
    if ft:
        return ft
    tag = _config_str(body, "sFTTag")
    if tag:
        m = re.search(r'value="([^"]+)"', tag) or re.search(r"value='([^']+)'", tag)
        if m:
            return html_lib.unescape(m.group(1))
    m = re.search(r'name="PPFT"[^>]*value="([^"]+)"', body) or re.search(
        r'value="([^"]+)"[^>]*name="PPFT"', body
    )
    return html_lib.unescape(m.group(1)) if m else ""


def _dump(name: str, text: str) -> Path:
    d = _ROOT / "debug_oauth"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(text or "", encoding="utf-8", errors="replace")
    logger.info("dump %s (%d bytes)", p.name, len(text or ""))
    return p


def _proxy_raw(raw: str) -> str:
    raw = (raw or "").strip() or os.environ.get("HTTP_PROXY", "").strip()
    if not raw:
        raise SystemExit("缺少代理：请设置 HTTP_PROXY 或传入 --proxy host:port:user:pass")
    return expand_proxy_template(raw, count=1)[0]


def _proxy_url(raw: str) -> str:
    cfg = parse_proxy(raw)
    return cfg.url if cfg else ""


def _parse_server_data(body: str) -> dict[str, Any]:
    idx = (body or "").find("var ServerData=")
    if idx < 0:
        return {}
    chunk = body[idx + len("var ServerData="):]
    try:
        data, _ = json.JSONDecoder().raw_decode(chunk)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _is_abuse_page(resp) -> bool:
    url = getattr(resp, "url", "") or ""
    body = getattr(resp, "text", "") or ""
    return (
        "/Abuse" in url
        or "urlTierRestore" in body
        or "fChallengeAbusiveAccount" in body
        or "Account_ServiceAbuseInterruptPage" in body
    )


def _clear_abuse(http: OutlookHttpSession, resp, proxy: str):
    """HAR：密码过了进 Abuse → risk/initialize → PX 按住 → risk/verify → TierRestore。"""
    import requests as _req

    body = resp.text or ""
    sd = _parse_server_data(body)
    if not sd and not _is_abuse_page(resp):
        return resp
    if not sd.get("urlTierRestore") and not _is_abuse_page(resp):
        return resp

    uaid = str(sd.get("sUnauthSessionID") or "")
    ctx = SignupSession(
        uaid=uaid or "0" * 32,
        signup_url="",
        signup_page_url=resp.url or "https://account.live.com/Abuse",
        cobrandid=str(sd.get("sCobrandId") or ""),
        contextid="",
        opid="",
        bk="",
        sru=str(sd.get("urlRU") or ""),
        canary=str(sd.get("apiCanary") or ""),
        hpgid=int(sd.get("hpgid") or 200252),
        mkt=str(sd.get("sMkt") or "EN-US"),
        lc="1033",
    )
    email = str(sd.get("sSigninName") or "")
    logger.info("Abuse 解封 email=%s siteId=%s reason=%s", email, sd.get("sSiteId"), sd.get("iAbuseReason"))

    abuse_origin = "https://account.live.com"
    risk_initialize(http, ctx, "", origin=abuse_origin)
    if ctx.human_sensor_url or sd.get("urlHumanIframe"):
        if not ctx.human_sensor_url:
            ctx.human_sensor_url = str(sd.get("urlHumanIframe") or "")
        load_human_sensor(http, ctx)
    if not ctx.continuation_token:
        raise RuntimeError("Abuse risk/initialize 无 continuationToken")

    # HAR（outlook登录.har entry 121，verify#1 成功样本）：px3/pxde 为空，但 pxvid 是
    # 真实 PX 访客 id（浏览器里由 PX 的 JS 生成并写入 _pxvid cookie）。提交带值的 silent
    # px3/pxde 反而会被判成篡改 → AADSTS7005106 riskBlock（见 entry 60 account A）。
    warmup_px_session(http, ctx)
    try:
        http.post(
            "https://collector-pxzc5j78di.hsprotect.net/api/v2/msft",
            headers={
                "Origin": "https://iframe.hsprotect.net",
                "Referer": "https://iframe.hsprotect.net/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"payload": ""},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("PX collector /msft skip: %s", exc)
    # 与注册同一套：verify#1 提交 silent _px3/_pxde/_pxvid，才能下发按压挑战而不是直接 riskBlock。
    silent = _acquire_silent_px(http, ctx, mode="solver", proxy=proxy, country="US")
    logger.info(
        "Abuse verify#1 silent pxvid=%s px3=%s",
        (silent.get("pxvid") or "")[:20],
        (silent.get("px3") or "")[:16],
    )
    restore_sig = {
        "puid": str(sd.get("sEncryptedPUID") or ""),
        "siteId": "00000000487A244A",
        "uiFlavor": "Web",
        "appId": "00000000487A244A",
        "action": "TierRestore",
        "memberName": email,
    }
    try:
        v1 = risk_verify(
            http, ctx,
            continuation_token=ctx.continuation_token,
            risk_provider_metadata=build_px_metadata(silent),
            msa_risk_verify_signature=restore_sig,
            origin=abuse_origin,
        )
    except _req.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 403:
            raise RuntimeError(f"Abuse riskBlock: {(exc.response.text or '')[:180]}") from exc
        raise RuntimeError(f"Abuse risk/verify #1 HTTP {exc.response.status_code if exc.response else '?'}: {(exc.response.text or '')[:180] if exc.response else exc}") from exc

    challenge = v1.get("challengeDetails") or {}
    if challenge.get("challengeType"):
        meta = challenge.get("challengeMetadata") or {}
        ctx.px_challenge_meta = meta
        load_challenge_iframe(http, ctx, meta)
        preload_px_challenge_assets(http, ctx, meta)
        warmup_px_session(http, ctx)
        time.sleep(1.0)
        post_px_beacon(http, ctx, tag="abuse-pre-press")
        post_px_bundle(http, ctx, tag="abuse-pre-press")
        _acquire_silent_px(http, ctx, mode="solver", proxy=proxy, country="US")
        press = _solve_px_protocol(http, ctx, phase="press", proxy=proxy, challenge_meta=meta, country="US")
        v2 = risk_verify(
            http, ctx,
            continuation_token=ctx.continuation_token,
            risk_provider_metadata=build_px_metadata(press),
            challenge_solution=build_challenge_solution(
                press, meta, challenge_type=str(challenge.get("challengeType") or "HumanCaptcha"),
            ),
            origin=abuse_origin,
        )
        if v2.get("challengeDetails"):
            raise RuntimeError("Abuse PX 按住未过，仍要挑战")
        logger.info("Abuse risk/verify #2 通过")
    else:
        logger.info("Abuse risk/verify #1 无挑战 keys=%s", list(v1.keys()))

    restore_url = str(sd.get("urlTierRestore") or "https://account.live.com/API/TierRestore")
    # HAR entry 199：body 除 siteId/netId/continuationToken 外，还带 memberName + 遥测四件套
    # (uiflvr/scid/uaid/hpgid)；headers 还带 canary/correlationId/client-request-id/hpgid/hpgact。
    payload = {
        "siteId": str(sd.get("sSiteId") or "292841"),
        "netId": str(sd.get("sEncryptedPUID") or ""),
        "continuationToken": ctx.continuation_token,
        "memberName": email,
        "uiflvr": 1001,
        "scid": 100121,
        "uaid": ctx.uaid,
        "hpgid": ctx.hpgid,
    }
    if not payload["netId"]:
        raise RuntimeError("Abuse 页无 sEncryptedPUID，无法 TierRestore")
    logger.info("POST TierRestore siteId=%s", payload["siteId"])
    restored = http.post(
        restore_url,
        json=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Origin": "https://account.live.com",
            "Referer": resp.url or "https://account.live.com/Abuse",
            "Accept": "application/json",
            "canary": ctx.canary,
            "correlationId": ctx.uaid,
            "client-request-id": ctx.uaid,
            "hpgid": str(ctx.hpgid),
            "hpgact": "0",
        },
    )
    _dump("rescue_tier_restore.json", restored.text or "")
    if restored.status_code >= 400:
        raise RuntimeError(f"TierRestore HTTP {restored.status_code}: {(restored.text or '')[:180]}")

    ru = str(sd.get("urlRU") or "")
    if ru:
        logger.info("Abuse 解封后续跳 urlRU")
        return http.get(ru, allow_redirects=True)
    return restored
def _decode_wlx(s: str) -> str:
    """还原 account.live.com WLXAccount 页的 JS 转义（\\u0026 / \\u002f / \\u003d 等）。"""
    try:
        return s.encode("utf-8").decode("unicode_escape")
    except Exception:  # noqa: BLE001
        return s


def _is_unfamiliar_location(body: str) -> bool:
    """「Help us protect your account」UnfamiliarLocationHard 页（/identity/confirm）。"""
    return (
        "UnfamiliarLocationHard" in body
        or ("rawProofList" in body and "proofPurpose" in body)
    )


def _extract_confirm_identity(body: str) -> dict[str, Any]:
    """从 /identity/confirm 提取 SendOtt / VerifyCode 所需参数。

    请求体（反向 acctcdn confirmidentity.js 坐实）：
      SendOtt    = {token, purpose, epid, autoVerification, autoVerificationFailed,
                    confirmProof?, HFId..HPId}
      VerifyCode = {code, action:"IptVerify", purpose, epid, confirmProof?}
    首次 SendOtt 的 token 为空（页面无 token，由服务器会话 cookie 识别），
    VerifyCode 响应返回 {proofData, token, route} 供续跳。
    """
    def _s(key: str) -> str:
        m = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
        return _decode_wlx(m.group(1)) if m else ""

    info: dict[str, Any] = {}
    m = re.search(r'"sendOtt"\s*:\s*\{[^}]*?"url"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
    info["send_ott_url"] = _decode_wlx(m.group(1)) if m else ""
    m = re.search(r'"verifyCode"\s*:\s*\{[^}]*?"url"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
    info["verify_code_url"] = _decode_wlx(m.group(1)) if m else ""
    info["purpose"] = _s("proofPurpose") or "UnfamiliarLocationHard"
    info["verify_action"] = _s("verifyProofAction") or "IptVerify"
    info["api_canary"] = _s("apiCanary")
    info["token"] = _s("token")
    m = re.search(r'"hpgid"\s*:\s*(\d+)', body)
    info["hpgid"] = m.group(1) if m else "200355"
    proofs: list[dict[str, Any]] = []
    m = re.search(r'"rawProofList"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
    if m:
        try:
            arr = json.loads(_decode_wlx(m.group(1)))
            proofs = arr if isinstance(arr, list) else []
        except json.JSONDecodeError:
            proofs = []
    info["proofs"] = proofs
    email_proof = next(
        (p for p in proofs if p.get("channel") == "Email" and p.get("epid")),
        None,
    )
    info["epid"] = (email_proof or {}).get("epid", "")
    info["proof_name"] = (email_proof or {}).get("name", "")
    return info


def _handle_unfamiliar_location(
    http: OutlookHttpSession,
    resp,
    proxy: str,
    email: str,
    recovery_email: str,
    uaid: str,
):
    """UnfamiliarLocationHard：SendOtt → CF 读码 → VerifyCode → 续跳。"""
    body = resp.text or ""
    info = _extract_confirm_identity(body)
    if not info["epid"] or not info["send_ott_url"] or not info["verify_code_url"]:
        raise RuntimeError(
            "UnfamiliarLocationHard 页缺字段: "
            + json.dumps({k: (v[:24] if isinstance(v, str) else v) for k, v in info.items()})
        )
    if not recovery_email:
        raise RuntimeError("UnfamiliarLocationHard 需要 recovery_email 收码（页内仅掩码名）")

    page_url = resp.url or "https://account.live.com/identity/confirm"
    origin = "https://account.live.com"
    hdrs = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "Origin": origin,
        "Referer": page_url,
        "canary": info["api_canary"],
        "client-request-id": uaid,
        "correlationId": uaid,
        "x-ms-correlation-id": uaid,
        "hpgid": info["hpgid"],
        "hpgact": "0",
    }

    # 1) 快照 CF 已有邮件 id（去重旧码）
    client = None
    before_ids: set[str] = set()
    try:
        from outlook_api_reg.cf_domain_mail import CFDomainMailClient, load_config

        client = CFDomainMailClient(load_config())
        before_ids = client.snapshot_ids(recovery_email)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CF 快照失败（继续但不保证去重旧码）: %s", exc)

    # 2) SendOtt：触发发码（token 初始为空，服务器按会话 cookie 识别）
    send_body = {
        "token": info["token"] or "",
        "purpose": info["purpose"],
        "epid": info["epid"],
        "autoVerification": False,
        "autoVerificationFailed": False,
    }
    logger.info(
        "UnfamiliarLocationHard SendOtt epid=%.24s… purpose=%s proof=%s",
        info["epid"], info["purpose"], info["proof_name"],
    )
    logger.info("SendOtt canary=%.40s… hpgid=%s uaid=%s", info["api_canary"], info["hpgid"], uaid[:12])
    _cookies = sorted(http.session.cookies.get_dict().keys())
    logger.info("SendOtt 前 cookies(%d)=%s", len(_cookies), ",".join(_cookies[:24]))
    sent = http.post(info["send_ott_url"], json=send_body, headers=hdrs, allow_redirects=False)
    _dump("rescue_send_ott.json", sent.text or "")
    logger.info("SendOtt status=%s body=%s", sent.status_code, (sent.text or "")[:180])
    if sent.status_code >= 400:
        raise RuntimeError(f"SendOtt HTTP {sent.status_code}: {(sent.text or '')[:180]}")
    # 服务器返回 {"error":{"code":...}} 时立即失败，不要白等 150s CF 读码
    try:
        _sj = sent.json() if sent.text else {}
        if isinstance(_sj, dict) and _sj.get("error"):
            raise RuntimeError(f"SendOtt 服务端错误: {_sj['error']}")
    except (ValueError, RuntimeError) as _e:
        if isinstance(_e, RuntimeError):
            raise
    except Exception:  # noqa: BLE001
        pass

    # 3) 读 CF 验证码（只读快照之后的新邮件，since_ts=现在）
    if client is None:
        raise RuntimeError("CF 收码客户端不可用")
    code = client.read_security_code(
        recovery_email, before_ids=before_ids, since_ts=time.time(), timeout=150,
    )
    if not code:
        raise RuntimeError("UnfamiliarLocationHard 未读到恢复邮箱验证码")

    # 4) VerifyCode
    verify_body = {
        "code": code,
        "action": info["verify_action"],
        "purpose": info["purpose"],
        "epid": info["epid"],
    }
    logger.info("UnfamiliarLocationHard VerifyCode code=%s action=%s", code, info["verify_action"])
    vr = http.post(info["verify_code_url"], json=verify_body, headers=hdrs, allow_redirects=False)
    _dump("rescue_verify_code.json", vr.text or "")
    logger.info("VerifyCode status=%s body=%s", vr.status_code, (vr.text or "")[:180])

    # 5) 续跳：VerifyCode 成功返回 {proofData, token, route}
    route = ""
    try:
        route = str((vr.json() or {}).get("route") or "")
    except Exception:  # noqa: BLE001
        m = re.search(r'"route"\s*:\s*"((?:[^"\\]|\\.)*)"', vr.text or "")
        route = _decode_wlx(m.group(1)) if m else ""
    if route:
        logger.info("VerifyCode 成功 route=%s", route[:120])
        return http.get(urllib.parse.urljoin(origin, route), allow_redirects=True)
    return vr


def _advance_after_login(
    http: OutlookHttpSession, resp, proxy: str, uaid: str,
    proof_meta: Optional[dict[str, str]] = None,
    email: str = "",
    recovery_email: str = "",
):
    if _is_abuse_page(resp):
        resp = _clear_abuse(http, resp, proxy)
        _dump("rescue_after_abuse.html", resp.text or "")
    ctx = _minimal_ctx(uaid)
    resp = follow_auto_post_forms(
        http, resp, tag="rescue", max_hops=12, enable_proof_pool=False,
        proof_meta=proof_meta, ctx=ctx,
    )
    if _is_unfamiliar_location(resp.text or ""):
        logger.info("命中 UnfamiliarLocationHard「Help us protect your account」→ OTT 处理")
        resp = _handle_unfamiliar_location(http, resp, proxy, email, recovery_email, uaid)
        _dump("rescue_after_unfamiliar.html", resp.text or "")
        resp = follow_auto_post_forms(
            http, resp, tag="afterunfamiliar", max_hops=12, enable_proof_pool=False,
            proof_meta=proof_meta, ctx=ctx,
        )
    return resp


def _apply_proof_meta(data: dict[str, Any], proof_meta: dict[str, str]) -> None:
    if not proof_meta:
        return
    rec_e = (proof_meta.get("recovery_email") or "").strip()
    rec_p = (proof_meta.get("recovery_password") or "").strip()
    if rec_e:
        data["recovery_email"] = rec_e
    if rec_p:
        data["recovery_password"] = rec_p
    for key in ("proofs_method", "proofs_satisfied"):
        if proof_meta.get(key):
            data[key] = proof_meta[key]


def _load_invalid_file(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "----" not in line:
            continue
        parts = line.split("----")
        email = (parts[0] if parts else "").strip()
        if not email or "@" not in email:
            continue
        rows.append({
            "email": email,
            "password": parts[1].strip() if len(parts) > 1 else "",
            "recovery_email": parts[2].strip() if len(parts) > 2 else "",
            "recovery_password": parts[3].strip() if len(parts) > 3 else "",
        })
    return rows


def _load_account(email: str) -> tuple[Optional[Path], dict[str, Any]]:
    email = email.strip().lower()
    try:
        from outlook_api_reg.account_store import get_account
    except ImportError:
        get_account = None  # type: ignore[assignment]
    if get_account is not None:
        acc = get_account(email)
        if acc:
            return None, acc
    for fp in sorted(ACCOUNTS_DIR.glob("*_at_outlook_com_*.json")):
        data = json.loads(fp.read_text(encoding="utf-8"))
        if (data.get("email") or "").strip().lower() == email:
            return fp, data
    for row in _load_invalid_file(ACCOUNTS_DIR / "invalid_for_browser.txt"):
        if row["email"].lower() == email:
            return None, row
    raise FileNotFoundError(f"accounts/ 无 {email}")


def _replace_combo_line(path: Path, email: str, new_line: str) -> bool:
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    changed = False
    out: list[str] = []
    for line in lines:
        head = line.split("----")[0].strip() if "----" in line else ""
        if head.lower() == email.lower() and not line.strip().startswith("#"):
            out.append(new_line)
            changed = True
        else:
            out.append(line)
    if not changed:
        out.append(new_line)
        changed = True
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def _writeback(fp: Path, data: dict[str, Any], rt: str, scope: str = "", *, out: Optional[dict[str, Any]] = None) -> None:
    email = data["email"]
    pwd = data.get("password", "")
    cid = data.get("client_id") or MAIL_CLIENT_ID
    rec_e = data.get("recovery_email") or ""
    rec_p = data.get("recovery_password") or ""
    now = datetime.now(timezone.utc).isoformat()
    _bump_rescue_stats(data, out or {})
    if rt:
        combo4 = "----".join([email, pwd, cid, rt])
        data["refresh_token"] = rt
        data["client_id"] = cid
        data["combo"] = combo4
        data["rescued_at"] = now
        data["last_alive_at"] = now
        data["rescued_scope"] = scope
        _replace_combo_line(ACCOUNTS_DIR / "accounts.txt", email, combo4)
    data["updated_at"] = now
    if rec_e:
        rt_seg = rt or data.get("refresh_token") or ""
        data["combo_recovery"] = "----".join([email, pwd, cid, rt_seg, rec_e, rec_p])
        if rt_seg:
            _replace_combo_line(ACCOUNTS_DIR / "accounts_recovery.txt", email, data["combo_recovery"])
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "已回写 %s%s%s",
        fp.name,
        " / accounts.txt" if rt else "",
        " / accounts_recovery.txt" if rec_e and (rt or data.get("refresh_token")) else "",
    )


def _bump_rescue_stats(data: dict[str, Any], out: dict[str, Any]) -> None:
    """每次走 rescue_login 落盘时累计次数与最近结果。"""
    n = int(data.get("rescue_count") or 0) + 1
    data["rescue_count"] = n
    data["last_rescue_at"] = datetime.now(timezone.utc).isoformat()
    data["last_rescue_ok"] = bool(out.get("ok"))
    reason = str(out.get("reason") or out.get("message") or "").strip()
    if out.get("ok"):
        data["last_rescue_reason"] = ""
    elif reason:
        data["last_rescue_reason"] = reason[:240]


def persist_rescue_outcome(
    email: str,
    data: dict[str, Any],
    out: dict[str, Any],
    *,
    accounts_dir: Optional[Path] = None,
) -> Optional[Path]:
    """将 rescue_one 结果写回 SQLite（或遗留 JSON）。"""
    del accounts_dir
    email = (email or data.get("email") or "").strip()
    if not email:
        return None
    try:
        from outlook_api_reg.account_store import write_rescue_outcome

        for key in ("recovery_email", "recovery_password", "proofs_method", "proofs_satisfied"):
            if out.get(key):
                data[key] = out[key]
        path_str = write_rescue_outcome(email, data, out)
        return Path(path_str) if path_str else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("SQLite 救援回写失败，回退 JSON: %s", exc)
    data = dict(data)
    data["email"] = email
    fp: Optional[Path] = None
    json_hint = data.get("_json") or data.get("source")
    if json_hint and Path(str(json_hint)).suffix == ".json":
        fp = Path(json_hint)
    if fp is None or not fp.exists():
        try:
            fp, existing = _load_account(email)
            data = {**existing, **data}
        except FileNotFoundError:
            fp = _ensure_account_json(email, data)
    if fp is None:
        fp = _ensure_account_json(email, data)
    _apply_proof_meta(data, {
        k: str(v) for k, v in out.items()
        if k in ("recovery_email", "recovery_password", "proofs_method", "proofs_satisfied") and v
    })
    rt = out.get("refresh_token", "") if out.get("ok") else ""
    _writeback(fp, data, rt or "", out.get("scope") or "", out=out)
    return fp


def rescue_and_persist(
    email: str,
    password: str,
    proxy: str,
    *,
    recovery_email: str = "",
    write: bool = True,
    accounts_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """单号救援：跑 rescue_one 并按需落盘（供 Web / CLI 共用）。"""
    try:
        out = rescue_one(email, password, proxy, recovery_email=recovery_email or "")
    except Exception as exc:  # noqa: BLE001
        logger.exception("救回异常 %s", email)
        out = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    out["email"] = email
    if write:
        try:
            _, data = _load_account(email)
        except FileNotFoundError:
            data = {
                "email": email,
                "password": password,
                "recovery_email": recovery_email or "",
            }
        data["password"] = password or data.get("password", "")
        if recovery_email:
            data["recovery_email"] = recovery_email
        persist_rescue_outcome(email, data, out, accounts_dir=accounts_dir)
        out["written"] = True
        try:
            _, saved = _load_account(email)
            out["rescue_count"] = int(saved.get("rescue_count") or 0)
        except FileNotFoundError:
            pass
    slim = {k: v for k, v in out.items() if k != "refresh_token" or not out.get("ok")}
    logp = (accounts_dir or ACCOUNTS_DIR) / "rescue_results.jsonl"
    logp.parent.mkdir(parents=True, exist_ok=True)
    with logp.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**slim, "rescue_count_hint": True}, ensure_ascii=False, default=str) + "\n")
    return out


def count_rescues_from_log(accounts_dir: Optional[Path] = None) -> dict[str, int]:
    """从 rescue_results.jsonl 统计历史救援次数（补旧数据）。"""
    logp = (accounts_dir or ACCOUNTS_DIR) / "rescue_results.jsonl"
    counts: dict[str, int] = {}
    if not logp.exists():
        return counts
    for line in logp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        email = str(row.get("email") or "").strip().lower()
        if email:
            counts[email] = counts.get(email, 0) + 1
    return counts


def _diagnose(resp) -> str:
    url = resp.url or ""
    body = resp.text or ""
    err = _config_str(body, "sErrorCode") or _config_str(body, "sErrTxt")
    low = (url + " " + body[:2000]).lower()
    bits = [f"url={url[:180]}"]
    if err:
        bits.append(f"sErrorCode={err}({_ERR.get(err, '?')})")
    if "hip" in low or "arkose" in low or "funcaptcha" in low:
        bits.append("验证码/HIP")
    if "perimeterx" in low or "px-captcha" in low or "hsprotect" in low:
        bits.append("PX")
    if "abuse" in low or "locked" in low or "compromised" in low:
        bits.append("封号/风控")
    if "identity" in low or "interrupt" in low or "proofs" in low or "recover" in low:
        bits.append("身份验证/插页")
    if "kmsi" in low:
        bits.append("KMSI")
    if "consent" in low or "srawinputscopes" in body:
        bits.append("同意页")
    if "passwd" in low or "password" in low:
        bits.append("仍在密码页")
    title = re.search(r"<title>([^<]+)</title>", body, re.I)
    if title:
        bits.append(f"title={title.group(1).strip()[:60]}")
    return " | ".join(bits)


def _extract_code(url: str) -> str:
    m = re.search(r"[?&]code=([^&]+)", url or "")
    return urllib.parse.unquote(m.group(1)) if m else ""


def _minimal_ctx(uaid: str) -> SignupSession:
    return SignupSession(
        uaid=uaid or "0" * 32,
        signup_url="",
        signup_page_url="https://login.live.com/",
        cobrandid="",
        contextid="",
        opid="",
        bk="",
        sru="",
        canary="",
        mkt="EN-US",
        lc="1033",
    )


def _get_credential_type(http: OutlookHttpSession, body: str, email: str, uaid: str) -> dict[str, Any]:
    ft = _ppft(body)
    gct = _config_str(body, "urlGetCredentialType") or "https://login.live.com/GetCredentialType.srf"
    url = urllib.parse.urljoin("https://login.live.com/", gct)
    if "uaid=" not in url and uaid:
        url += ("&" if "?" in url else "?") + "uaid=" + uaid
    payload = {
        "username": email,
        "uaid": uaid,
        "isOtherIdpSupported": True,
        "checkPhones": False,
        "isRemoteNGCSupported": True,
        "isCookieBannerShown": False,
        "isFidoSupported": True,
        "forceotclogin": False,
        "otclogindisallowed": False,
        "isExternalFederationDisallowed": False,
        "isRemoteConnectSupported": False,
        "federationFlags": 3,
        "isSignup": False,
        "flowToken": ft,
    }
    try:
        r = http.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "Origin": "https://login.live.com",
                "Referer": "https://login.live.com/",
            },
        )
        return r.json() if r.headers.get("content-type", "").startswith("application/json") or r.text[:1] == "{" else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("GetCredentialType 失败: %s", exc)
        return {}


def _post_password(http: OutlookHttpSession, resp, email: str, password: str):
    body = resp.text or ""
    url_post = _config_str(body, "urlPost")
    if not url_post:
        raise RuntimeError("登录页无 urlPost，无法提交密码: " + _diagnose(resp))
    action = urllib.parse.urljoin(resp.url or "https://login.live.com/", url_post)
    ft = _ppft(body)
    if not ft:
        raise RuntimeError("登录页未解析到 PPFT: " + _diagnose(resp))
    ft_name = _config_str(body, "sFTName") or "PPFT"
    canary = _config_str(body, "canary") or _config_str(body, "sCanary")
    fields = {
        "i13": "0",
        "login": email,
        "loginfmt": email,
        "type": "11",
        "LoginOptions": "3",
        "passwd": password,
        "ps": "2",
        "PPFT": ft,
        "PPSX": "Passp",
        "NewUser": "1",
        "FoundMSAs": "",
        "fspost": "0",
        "i21": "0",
        "CookieDisclosure": "0",
        "IsFidoSupported": "1",
        "isSignupPost": "0",
        "isRecoveryAttemptPost": "0",
        "i19": str(random.randint(4000, 18000)),
    }
    if ft_name and ft_name != "PPFT":
        fields[ft_name] = ft
    if canary:
        fields["canary"] = canary
    logger.info("提交密码登录 PPFT=%d → %s", len(ft), action[:120])
    return http.post(
        action,
        data=fields,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://login.live.com",
            "Referer": resp.url or "https://login.live.com/",
        },
        allow_redirects=True,
    )


def _check_password(http: OutlookHttpSession, email: str, password: str, uaid: str = "", correlation_id: str = "") -> requests.Response:
    """checkpassword.srf：密码检查（浏览器登录第 1 步，协议版之前漏了这步）。"""
    url = "https://login.live.com/checkpassword.srf"
    body = {
        "username": email,
        "password": password,
        "checkpasswordflowtoken": "",
    }
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://login.live.com",
        "Referer": "https://login.live.com/login.srf",
        "client-request-id": correlation_id or uaid,
        "correlationid": correlation_id or uaid,
        "hpgact": "0",
        "hpgid": "33",
    }
    logger.info("checkpassword.srf → 密码检查")
    return http.post(url, json=body, headers=hdrs, allow_redirects=True)


def _gct_proofs(gct: dict[str, Any]) -> list[dict[str, Any]]:
    creds = (gct.get("Credentials") or {}) if isinstance(gct, dict) else {}
    proofs = creds.get("OtcLoginEligibleProofs") or []
    return proofs if isinstance(proofs, list) else []


def _try_otc_login(http: OutlookHttpSession, resp, email: str, recovery_email: str, gct: dict[str, Any]):
    """PrefCredential=3 时走一次性验证码登录，用已绑 your-cf-domain.com 收码。"""
    if not recovery_email or not recovery_email.endswith("@your-cf-domain.com"):
        return None
    proofs = _gct_proofs(gct)
    if not proofs:
        # 登录页 ServerData.oGetCredTypeResult 里也可能带
        body = resp.text or ""
        m = re.search(r'"OtcLoginEligibleProofs"\s*:\s*(\[.*?\])\s*,\s*"PrefCredential"', body)
        if m:
            try:
                proofs = json.loads(m.group(1))
            except json.JSONDecodeError:
                proofs = []
    proof = next((p for p in proofs if p.get("otcEnabled") and p.get("data")), None)
    if not proof:
        logger.info("无 OTC 可用 proof，跳过验证码登录")
        return None
    try:
        from outlook_api_reg.cf_domain_mail import CFDomainMailClient, load_config
    except Exception as exc:  # noqa: BLE001
        logger.warning("CF 收码模块不可用: %s", exc)
        return None
    cfg = load_config()
    ok, err = cfg.is_valid()
    if not ok:
        logger.warning("CF 配置无效: %s", err)
        return None
    client = CFDomainMailClient(cfg)
    before = client.snapshot_ids(recovery_email)
    body = resp.text or ""
    url_post = _config_str(body, "urlPost")
    if not url_post:
        return None
    action = urllib.parse.urljoin(resp.url or "", url_post)
    ft = _ppft(body)
    fields = {
        "login": email,
        "loginfmt": email,
        "type": "19",
        "PPFT": ft,
        "PPSX": "Passp",
        "NewUser": "1",
        "SentProofIDE": proof.get("data", ""),
        "otcLogin": "true",
        "i19": str(random.randint(4000, 18000)),
    }
    logger.info("请求 OTC → %s display=%s", action[:100], proof.get("display"))
    sent = http.post(action, data=fields, allow_redirects=True)
    _dump("rescue_otc_sent.html", sent.text or "")
    logger.info("等待恢复邮箱 %s 验证码…", recovery_email)
    code = client.read_security_code(recovery_email, before_ids=before, timeout=120)
    if not code:
        return None
    body2 = sent.text or ""
    ft2 = _ppft(body2) or ft
    action2 = urllib.parse.urljoin(sent.url or "", _config_str(body2, "urlPost") or url_post)
    submit = {
        "login": email,
        "loginfmt": email,
        "type": "22",
        "PPFT": ft2,
        "otcLogin": "true",
        "otc": code,
        "iOttText": code,
        "SentProofIDE": proof.get("data", ""),
        "i19": str(random.randint(4000, 18000)),
    }
    logger.info("提交 OTC=%s", code)
    nxt = http.post(action2, data=submit, allow_redirects=True)
    _dump("rescue_otc_submit.html", nxt.text or "")
    return nxt


def rescue_one(email: str, password: str, proxy: str, *, recovery_email: str = "") -> dict[str, Any]:
    os.environ.setdefault("OUTLOOK_OAUTH_DEBUG", "1")
    http = OutlookHttpSession(proxy=proxy)
    http.session.headers["Accept-Language"] = "en-US,en;q=0.9"
    proof_meta: dict[str, str] = {}
    params = {
        "client_id": MAIL_CLIENT_ID,
        "scope": GRAPH_MAIL_SCOPE,
        "redirect_uri": MAIL_REDIRECT_URI,
        "response_type": "code",
        "response_mode": "query",
        "login_hint": email,
        "prompt": "login",
        "msproxy": "1",
        "issuer": "mso",
        "tenant": "common",
        "ui_locales": "en-US",
        "mkt": "EN-US",
        "lc": "1033",
    }
    auth = "https://login.live.com/oauth20_authorize.srf?" + urllib.parse.urlencode(params)
    logger.info("GET authorize (Graph scope) login_hint=%s", email)
    resp = http.get(auth, allow_redirects=True)
    _dump("rescue_authorize.html", resp.text or "")
    code = _extract_code(resp.url or "")
    uaid = _config_str(resp.text or "", "sUnauthSessionID") or _config_str(resp.text or "", "sSessionId") or ""
    if not code:
        gct = _get_credential_type(http, resp.text or "", email, uaid)
        exists = gct.get("IfExistsResult")
        creds = gct.get("Credentials") or {}
        pref = creds.get("PrefCredential")
        logger.info(
            "GetCredentialType IfExists=%s PrefCredential=%s ErrorHR=%s keys=%s",
            exists, pref, gct.get("ErrorHR"), list(gct.keys())[:10],
        )
        if exists == 1:
            return {"ok": False, "reason": "账号不存在(GetCredentialType IfExistsResult=1)", "diag": _diagnose(resp)}

        def _device_blocked(r) -> bool:
            t = (r.text or "").lower()
            return "different device" in t or "other authentication method" in t

        used_otc = False
        if str(pref) == "3" and recovery_email:
            logger.info("PrefCredential=3，优先走恢复邮箱 OTC（不再先打密码）")
            otc_resp = _try_otc_login(http, resp, email, recovery_email, gct)
            if otc_resp is not None:
                resp = otc_resp
                used_otc = True
            else:
                logger.warning("OTC 未发出")

        if not used_otc:
            # 浏览器登录第 1 步：checkpassword.srf（密码检查），漏了这步会导致 ppsecure 循环
            _uaid = (resp.url or "").split("uaid=")[-1].split("&")[0] if "uaid=" in (resp.url or "") else ""
            _corr = _config_str(resp.text or "", "correlationId")
            try:
                _check_password(http, email, password, _uaid, _corr)
            except Exception as _e:
                logger.warning("checkpassword.srf 失败（继续原流程）: %s", _e)
            resp = _post_password(http, resp, email, password)
            _dump("rescue_after_password.html", resp.text or "")
            err = _config_str(resp.text or "", "sErrorCode")
            if _device_blocked(resp):
                return {
                    "ok": False,
                    "reason": "密码登录被设备风控拦截（Please retry with a different device or other authentication method）",
                    "diag": (resp.text or "")[:240],
                }
            if err in _ERR:
                return {"ok": False, "reason": f"{_ERR[err]} (sErrorCode={err})", "diag": _diagnose(resp)}

        try:
            resp = _advance_after_login(http, resp, proxy, uaid, proof_meta, email=email, recovery_email=recovery_email)
        except RuntimeError as exc:
            return {"ok": False, "reason": str(exc), "diag": _diagnose(resp), **proof_meta}
        if _is_abuse_page(resp):
            try:
                resp = _advance_after_login(http, resp, proxy, uaid, proof_meta, email=email, recovery_email=recovery_email)
            except RuntimeError as exc:
                return {"ok": False, "reason": str(exc), "diag": _diagnose(resp), **proof_meta}
        code = _extract_code(resp.url or "")

    if not code:
        # 会话可能已建立但 authorize 被插页打断 → 再走一遍消费者 authorize
        ctx = _minimal_ctx(uaid)
        mail = fetch_mail_oauth_code(
            http, ctx, email, client_id=MAIL_CLIENT_ID, scope=GRAPH_MAIL_SCOPE,
            proof_meta=proof_meta,
        )
        code = mail.get("code", "")
        if not code:
            dump = _ROOT / "debug_oauth" / f"rescue_{email.split('@')[0]}.html"
            dump.parent.mkdir(exist_ok=True)
            dump.write_text(resp.text or "", encoding="utf-8", errors="replace")
            return {
                "ok": False,
                "reason": "登录后未拿到 OAuth code",
                "diag": _diagnose(resp),
                "dump": str(dump),
                **proof_meta,
            }

    tok = exchange_code_for_token(http, code, client_id=MAIL_CLIENT_ID, scope=GRAPH_MAIL_SCOPE)
    rt = tok.get("refresh_token", "")
    scope = tok.get("scope", "")
    if not rt:
        return {
            "ok": False,
            "reason": f"换 token 无 refresh_token scope={scope}",
            "diag": _diagnose(resp),
            "token": tok,
            **proof_meta,
        }

    proxy_url = _proxy_url(proxy)
    probe = probe_token(email, rt, proxy_url=proxy_url)
    usable = probe.get("usable") or []
    granted = (probe.get("detail") or {}).get("granted_scope", scope)
    graph_ok = "graph" in usable
    return {
        "ok": graph_ok,
        "refresh_token": rt,
        "scope": granted,
        "usable": usable,
        "reason": "" if graph_ok else f"拿到 token 但 Graph 不可用 usable={usable} scope={granted}",
        "probe": probe,
        **proof_meta,
    }


def _ensure_account_json(email: str, data: dict[str, Any]) -> Path:
    slug = email.replace("@", "_at_").replace(".", "_")
    fp = ACCOUNTS_DIR / f"{slug}_rescued.json"
    if fp.exists():
        return fp
    seed = {
        "email": email,
        "password": data.get("password", ""),
        "recovery_email": data.get("recovery_email", ""),
        "recovery_password": data.get("recovery_password", ""),
        "client_id": MAIL_CLIENT_ID,
    }
    fp.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def main() -> int:
    parser = argparse.ArgumentParser(description="用账密重登拿 Graph refresh_token")
    parser.add_argument("emails", nargs="*", help="要救的邮箱")
    parser.add_argument("--file", default="", help="从 invalid 清单批量读 email----pwd----recovery")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个（0=全部）")
    parser.add_argument("--offset", type=int, default=0, help="跳过前 N 个")
    parser.add_argument("--proxy", default=os.environ.get("HTTP_PROXY", ""), help="host:port:user:pass 或含 {sid} 模板")
    parser.add_argument("--no-write", action="store_true", help="成功也不回写 accounts")
    parser.add_argument("--skip-done", action="store_true", help="跳过 rescue_results.jsonl 里已成功的号")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    jobs: list[dict[str, Any]] = []
    if args.file:
        jobs.extend(_load_invalid_file(Path(args.file)))
    for email in args.emails:
        fp, data = _load_account(email)
        data = dict(data)
        data["_json"] = str(fp) if fp else ""
        jobs.append(data)
    if args.offset:
        jobs = jobs[args.offset:]
    if args.skip_done:
        done: set[str] = set()
        logp = ACCOUNTS_DIR / "rescue_results.jsonl"
        if logp.exists():
            for line in logp.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("ok") and row.get("email"):
                    done.add(str(row["email"]).lower())
        if done:
            jobs = [j for j in jobs if (j.get("email") or "").lower() not in done]
            logger.info("跳过已成功 %s 个", len(done))
    if args.limit:
        jobs = jobs[: args.limit]
    if not jobs:
        print(json.dumps({"ok": False, "reason": "没有要处理的账号"}, ensure_ascii=False))
        return 2

    results = []
    for i, data in enumerate(jobs, 1):
        email = (data.get("email") or "").strip()
        pwd = data.get("password") or ""
        proxy = _proxy_raw(args.proxy)
        ok_p, msg_p = preflight_proxy(proxy)
        logger.info("=== [%s/%s] %s 代理预检=%s %s ===", i, len(jobs), email, ok_p, msg_p)
        if not ok_p:
            logger.warning("预检失败仍继续登录: %s", msg_p[:160])
        if not pwd:
            results.append({"email": email, "ok": False, "reason": "无密码"})
            continue
        out = rescue_and_persist(
            email, pwd, proxy,
            recovery_email=data.get("recovery_email") or "",
            write=not args.no_write,
        )
        slim = {k: v for k, v in out.items() if k != "refresh_token" or not out.get("ok")}
        results.append(slim)
        time.sleep(3)

    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return 0 if any(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
