"""批量浏览器解封 + 抓包：统计成功率 + 记录每个号的登录端点（供协议复刻分析）。"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from outlook_api_reg.roxy_browser import RoxyBrowserClient, roxy_cdp_session
from outlook_api_reg.cf_domain_mail import CFDomainMailClient, load_config

ACCOUNTS = [
    ("dzs9zrlmkq@outlook.com", "CanVOM#DtNP5f2Hl", "tl22nsocwqg7@fovts.dev"),
    ("e9bvnjngok@outlook.com", "$ZIGIKdJj@WA4Wpo", "l2pdq8pws1uc@fovts.dev"),
    ("i0zoywodnp@outlook.com", "OOf&3#YegETknMV%", "vfzs5ph1zphq@fovts.dev"),
    ("jk0pfz5pfc@outlook.com", "jETazgduePC4Bbfw", "dw46ic23naj0@fovts.dev"),
    ("kjOFVDipfcO@outlook.com", "i5PnhRYd", "wcei413a91p9@fovts.dev"),
    ("njtqct8ih5@outlook.com", "XNZriT8VNL8h7ru!", "lknnadl00vut@fovts.dev"),
    ("nzzrbkdri7@outlook.com", "JC8mrkNcS@QA0@z9", "s38e2k0vbfhk@fovts.dev"),
    ("pezn1hi4uy@outlook.com", "BJpd&h3pzR4boY4M", "llg69bjpts61@fovts.dev"),
    ("rnrhterko1@outlook.com", "OwPcBoBq4%5uMD&M", "qaagwzw2uenz@fovts.dev"),
    ("ttpmu2ptqg@outlook.com", "UU51@IBy7KEwUb0P", "uly19bl1zfbl@fovts.dev"),
    ("uNJLZWPCFJfqYIAz@outlook.com", "cHLErnSVulM7u8", "zu6wi8y8ktbj@fovts.dev"),
    ("wzkvisydiv@outlook.com", "sytlhy%LEb46jIyS", "cdduumvkrhuo@fovts.dev"),
    ("ejsay54qp6@outlook.com", "Abarykk6nLDxo!7", "zgi8xnps8y8g@fovts.dev"),
    ("mez3dkltar@outlook.com", "Ab6hzepPCmq0O!7", "q4xgxhwrpki2@fovts.dev"),
    ("pueurwzwzmz@outlook.com", "zuojw3bajm8a", "m0pi573zc1ot@fovts.dev"),
    ("szgzeypzffqi@outlook.com", "dtgu86min0f", "yaoayody215s@fovts.dev"),
    ("krmdubeovu@outlook.com", "4nlvsb3qe7qhx2", "snfi1tllfjfg@fovts.dev"),
]

SIGNIN = "https://login.live.com/login.srf?wa=wsignin1.0&wreply=https://outlook.live.com/mail/"
OUT = os.environ.get("RESCUE_BATCH_OUT", "accounts/rescue_batch_results.jsonl")

def _click(page, *sels):
    for sel in sels:
        try:
            b = page.locator(sel).first
            if b.count() and b.is_visible():
                b.click(timeout=4000); return True
        except Exception: pass
    return False

def _capture(page, store):
    def _req(req):
        try: post = req.post_data or ""
        except Exception: post = ""
        if "live.com" in req.url or "account.live.com" in req.url:
            store.append({"t":"req","m":req.method,"u":req.url,"p":(post or "")[:400]})
    def _resp(resp):
        if "live.com" in req.url if False else "live.com" in resp.url or "account.live.com" in resp.url:
            try: b = resp.text()[:200]
            except Exception: b = ""
            store.append({"t":"resp","s":resp.status,"u":resp.url,"b":b})
    page.on("request", _req)
    page.on("response", _resp)

def main():
    roxy = RoxyBrowserClient()
    if not roxy.health():
        print("Roxy 不可用", roxy.last_error); return
    pid = roxy.list_profiles()[0].id
    results = []
    for idx, (email, pwd, rec) in enumerate(ACCOUNTS, 1):
        store = []
        t0 = time.time()
        try:
            with roxy_cdp_session(pid, client=roxy, close_on_exit=True) as (_, context):
                page = context.new_page()
                _capture(page, store)
                page.goto(SIGNIN, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(1500)
                inp = page.locator('input[type="email"], input[name="loginfmt"]').first
                inp.wait_for(state="visible", timeout=30000)
                inp.fill(email); page.wait_for_timeout(300)
                _click(page, "#idSIButton9", "#iNext", 'button[type="submit"]')
                state = "failed"
                for _ in range(12):
                    page.wait_for_timeout(1500)
                    body = page.evaluate("() => document.body.innerText").lower()
                    url = page.url.lower()
                    if url.startswith("https://outlook.live.com") or url.startswith("https://account.live.com"):
                        state = "unlocked"; break
                    if "send code" in body or "verify your email" in body or "we'll send a code" in body:
                        for sel in ('input[type="email"]','input[type="text"]','input:not([type])'):
                            i2 = page.locator(sel).first
                            if i2.count() and i2.is_visible():
                                i2.fill(rec); break
                        page.wait_for_timeout(300)
                        since = time.time()
                        _click(page, 'button:has-text("Send code")', 'button:has-text("Next")', "#idSIButton9", "#iNext")
                        code = CFDomainMailClient(load_config()).read_security_code(rec, timeout=90, poll_interval=3.0, since_ts=since)
                        if code:
                            for sel in ('input[name="otc"]','#otc','input[autocomplete="one-time-code"]','input[type="text"]'):
                                i3 = page.locator(sel).first
                                if i3.count() and i3.is_visible():
                                    i3.fill(code); break
                            page.wait_for_timeout(300)
                            _click(page, 'button:has-text("Verify")', 'button:has-text("Next")', "#idSIButton9", "#iNext", 'input[type="submit"]')
                    elif "enter your password" in body or 'type="password"' in body:
                        p = page.locator('input[type="password"]').first
                        if p.count() and p.is_visible():
                            p.fill(pwd); page.wait_for_timeout(300)
                            _click(page, "#idSIButton9", "#iNext", 'button[type="submit"]')
                # 抓 GetOneTimeCode 的 State
                otc_state = ""
                for e in store:
                    if e["t"]=="resp" and "GetOneTimeCode" in e["u"]:
                        otc_state = e.get("b","")
                results.append({"email":email, "ok": state=="unlocked", "state":state, "otc_state":otc_state[:80], "elapsed":round(time.time()-t0,1)})
        except Exception as e:
            results.append({"email":email, "ok":False, "state":"error", "err":str(e)[:100], "elapsed":round(time.time()-t0,1)})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        with open(OUT, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False)+"\n")
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n=== 成功率 {ok}/{len(results)} ===")

if __name__ == "__main__":
    main()
