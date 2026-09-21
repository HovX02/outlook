#!/usr/bin/env python3
"""Run the pre-CAPTCHA Outlook signup path with native macOS keyboard input.

This is a reproducible local counterpart to the successful Codex Computer Use
experiment.  The risk-sensitive signup portion runs without Playwright/CDP and
types through the visible browser with macOS native events.  The default smoke
mode stops before account creation; ``--full`` continues through CAPTCHA, CF
proof, Outlook and OAuth/token.  Use ``--auto`` for a fully hands-off run.

Requirements:
  * macOS
  * RoxyBrowser running with its local API enabled
  * Terminal (or the Python executable) allowed under
    System Settings -> Privacy & Security -> Accessibility
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import string
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_DIR / ".env")
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config.constants import locale_for_country, signup_url_for_country
from service.browser.macos_native_ui import (
    NativeUIError,
    RoxyNativeUI,
    accessibility_trusted,
    responsible_app_hint,
)
from service.account.account_store import save_register_result
from service.resource.recovery.cf_domain_mail import CFDomainMailClient, allocate_address
from service.resource.proxy.proxy_utils import preflight_proxy, rewrite_ipwo_zone_country
from service.browser.roxy_browser import (
    RoxyBrowserClient,
    infer_zone_country,
    roxy_cdp_session,
)

# Native keyboard coordinates are calibrated against the English signup flow.
NATIVE_SIGNUP_URL = "https://signup.live.com/signup?lic=1&mkt=EN-US"


T = TypeVar("T")


class RegistrationBlocked(Exception):
    """Microsoft 风控拦截（Account creation blocked / unusual activity）。"""


class RegistrationError(Exception):
    """单次注册失败，外层可重试。"""


@dataclass
class BatchStats:
    target: int = 1
    successes: list[RegisterResult] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


def _retry_roxy(label: str, action: Callable[[], T], *, attempts: int = 4) -> T:
    """Retry transient Roxy local/cloud API failures with short backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except (OSError, RuntimeError) as exc:
            if attempt == attempts:
                raise
            delay = 1.5 * attempt
            print(f"[native] Roxy {label} 失败，{delay:.1f}s 后重试 "
                  f"({attempt}/{attempts}): {exc}")
            time.sleep(delay)
    raise AssertionError("unreachable")


def _load_roxy_settings_from_account_manager() -> None:
    if os.environ.get("ROXY_TOKEN") or os.environ.get("ROXY_API_KEY"):
        return
    candidates = [
        Path(os.environ.get("ACCOUNT_MANAGER_DB", "")).expanduser(),
        PROJECT_DIR.parent / "account_manager" / "account_manager.db",
        Path("/Users/flx/IdeaProjects/to-api/account_manager/account_manager.db"),
    ]
    for db_path in candidates:
        if not str(db_path) or not db_path.exists():
            continue
        try:
            with sqlite3.connect(str(db_path)) as conn:
                rows = dict(conn.execute(
                    "select key,value from system_settings where key in "
                    "('app.roxy_api','app.roxy_token','app.roxy_workspace_id')"
                ).fetchall())
            values = {key: json.loads(value) for key, value in rows.items()}
            if values.get("app.roxy_token"):
                os.environ.setdefault("ROXY_API", str(values.get("app.roxy_api") or "http://127.0.0.1:50000"))
                os.environ.setdefault("ROXY_TOKEN", str(values["app.roxy_token"]))
                if values.get("app.roxy_workspace_id"):
                    os.environ.setdefault("ROXY_WORKSPACE_ID", str(values["app.roxy_workspace_id"]))
                return
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            continue


def _rand(length: int) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _random_email_local(length: int = 0) -> str:
    """MS signup requires local part to start with a letter (not a digit)."""
    n = length or secrets.randbelow(3) + 10  # 10-12
    first = secrets.choice(string.ascii_lowercase)
    rest = "".join(secrets.choice(string.ascii_lowercase) for _ in range(n - 1))
    return first + rest


def _random_password(length: int = 0) -> str:
    n = length or secrets.randbelow(4) + 11  # 11-14
    chars = string.ascii_lowercase + string.digits
    while True:
        pwd = "".join(secrets.choice(chars) for _ in range(n))
        if any(c.islower() for c in pwd) and any(c.isdigit() for c in pwd):
            return pwd


def _gen_email(explicit: str = "") -> str:
    raw = (explicit or f"{_random_email_local()}@outlook.com").strip()
    local = raw.split("@", 1)[0]
    if not local or not local[0].isalpha():
        raise RegistrationError(
            f"邮箱 local 必须以字母开头: {raw!r}（微软会拒绝数字开头）"
        )
    return raw if "@" in raw else f"{raw}@outlook.com"


def _gen_password(explicit: str = "") -> str:
    return explicit or _random_password()


def _email_local_for_signup(email: str) -> str:
    """Signup UI has a separate @outlook.com dropdown; type local part only."""
    return email.split("@", 1)[0].strip()


_FIRST_NAMES = (
    "Adrian", "Evan", "Marcus", "Leo", "Noah", "Ethan", "Miles", "Owen",
    "Lucas", "Henry", "Felix", "Grant", "Caleb", "Derek", "Simon",
)
_LAST_NAMES = (
    "Silva", "Mercer", "Brooks", "Hayes", "Foster", "Reed", "Coleman",
    "Parker", "Bennett", "Murphy", "Santos", "Rivera", "Collins", "Turner",
)


def _random_identity() -> tuple[str, str]:
    return secrets.choice(_FIRST_NAMES), secrets.choice(_LAST_NAMES)


def _checkpoint(ui: RoxyNativeUI, label: str, *, auto: bool) -> None:
    print(f"[{label}] title={ui.title()!r}")
    print(f"[{label}] url={ui.url()}")
    if not auto:
        input(f"确认 Roxy 页面处于 {label}，按 Enter 继续：")
        ui._maybe_activate()
        time.sleep(0.3)


def _is_registration_blocked(ui: RoxyNativeUI) -> bool:
    title = ui.title().lower()
    if any(k in title for k in (
        "blocked",
        "unusual activity",
        "we ran into a problem",
        "ran into a problem",
        "rencontré un problème",
        "rencontré un probleme",
        "problem aufgetreten",
        "account creation has been blocked",
    )):
        return True
    url = ui.url().lower()
    return "blocked" in url


def _raise_if_blocked(ui: RoxyNativeUI, *, stage: str = "") -> None:
    if _is_registration_blocked(ui):
        prefix = f"{stage}: " if stage else ""
        raise RegistrationBlocked(f"{prefix}Microsoft 拦截: {ui.title()}")


def _settle_signup_home_page(
    ui: RoxyNativeUI,
    *,
    signup_url: str,
    auto: bool,
) -> None:
    """Ensure the active tab is the fresh signup page before typing email."""
    ui._maybe_activate()
    ui.activate_tab_with_url("signup.live.com/signup")
    _dismiss_save_password(ui)
    time.sleep(1.0)
    print(f"[邮箱首页] title={ui.title()!r}")
    print(f"[邮箱首页] url={ui.url()}")
    if not auto:
        input("确认 Roxy 页面处于邮箱首页，按 Enter 继续：")
        ui._maybe_activate()
        time.sleep(0.3)
    if _is_registration_blocked(ui):
        print("[native] 首页命中风控标题，原标签重载一次…")
        ui.open_signup_url(signup_url)
        time.sleep(2.5)
        ui.activate_tab_with_url("signup.live.com/signup")
        _dismiss_save_password(ui)
        time.sleep(1.0)
        print(f"[邮箱首页] title={ui.title()!r}")
        print(f"[邮箱首页] url={ui.url()}")
    _raise_if_blocked(ui, stage="邮箱首页")


def _dismiss_save_password(ui: RoxyNativeUI) -> None:
    """Chromium 的 Save password 气泡会挡住表单并抢焦点（含土耳其语等 locale）。"""
    if not ui.steal_focus and not ui.is_frontmost():
        return
    time.sleep(0.25)
    # Do not click guessed coordinates here: Roxy profiles can have different
    # window sizes and those clicks may land on the tab strip, switching away
    # from signup.live.com.  Escape closes the Chromium native bubble without
    # disturbing the active web tab.
    ui.press("Escape")
    time.sleep(0.3)
    ui.press("Escape")
    time.sleep(0.3)


def _is_proofs_step_url_or_title(*, url: str = "", title: str = "") -> bool:
    low_url = (url or "").lower()
    low_title = (title or "").lower()
    if "account.live.com/proofs" in low_url:
        return True
    if "account.live.com/interrupt/credentialaction" in low_url:
        return True
    if "protect your account" in low_title:
        return True
    return False


def _is_proofs_step(ui: RoxyNativeUI) -> bool:
    return _is_proofs_step_url_or_title(url=ui.url(), title=ui.title())


def _wait_for_post_captcha(ui: RoxyNativeUI, *, timeout: float = 45.0) -> None:
    """Wait until CAPTCHA completes and Microsoft redirects to proofs."""
    deadline = time.monotonic() + timeout
    last_url = ""
    last_title = ""
    while time.monotonic() < deadline:
        ui.activate_tab_with_url("signup.live.com/signup")
        last_url = ui.url()
        last_title = ui.title()
        if _is_proofs_step_url_or_title(url=last_url, title=last_title):
            return
        time.sleep(0.5)
    raise NativeUIError(
        f"timed out waiting for proofs after CAPTCHA; "
        f"last url={last_url!r}, title={last_title!r}"
    )


def _run_accessible_captcha(
    ui: RoxyNativeUI,
    *,
    attempts: int = 10,
    hold_seconds: float = 4.2,
) -> None:
    """Accessible CAPTCHA：Dismiss 密码气泡 → 图标 → 按住 → 确认；失败则整轮重试。"""
    _dismiss_save_password(ui)
    for captcha_attempt in range(1, attempts + 1):
        if _is_proofs_step(ui):
            return
        _dismiss_save_password(ui)
        ui.click_relative(355, 568)
        time.sleep(hold_seconds)
        _dismiss_save_password(ui)
        ui.click_relative(484, 568)
        time.sleep(1.5)
        try:
            _wait_for_post_captcha(ui, timeout=25)
            return
        except NativeUIError:
            if _is_proofs_step(ui):
                return
            if captcha_attempt >= attempts:
                raise RegistrationError("CAPTCHA 多次重试仍未通过") from None
            print(
                f"[native] CAPTCHA 未跳转（可能 Please try again），"
                f"重试 ({captcha_attempt}/{attempts})"
            )
            time.sleep(1.5)
            _dismiss_save_password(ui)


def _resolve_profile(roxy: RoxyBrowserClient, profile_id: str, profile_name: str) -> str:
    if profile_id:
        return profile_id
    matches = [profile for profile in roxy.list_profiles() if profile.name == profile_name]
    if not matches:
        raise SystemExit(f"未找到 Roxy profile: {profile_name}")
    return matches[0].id


def _reset_profile(roxy: RoxyBrowserClient, profile_id: str) -> None:
    """关闭并清空 Cookie，为下一次尝试换全新会话。"""
    try:
        _retry_roxy("关闭 profile", lambda: roxy.close(profile_id))
    except (OSError, RuntimeError):
        pass
    time.sleep(1.2)
    _retry_roxy("清理 Cookie/缓存", lambda: roxy.clear_profile_cookies(profile_id))
    time.sleep(0.8)
    _retry_roxy("打开 profile", lambda: roxy.open(profile_id))
    time.sleep(3.5)


def _is_transient_ui_error(exc: NativeUIError) -> bool:
    msg = str(exc)
    lowered = msg.lower()
    return (
        "-1712" in msg
        or "-609" in msg
        or "连接无效" in msg
        or "timed out" in lowered
    )


def _ui_with_retry(ui: RoxyNativeUI, action: Callable[[], T], *, attempts: int = 4) -> T:
    for attempt in range(1, attempts + 1):
        try:
            ui._maybe_activate()
            return action()
        except NativeUIError as exc:
            if attempt >= attempts or not _is_transient_ui_error(exc):
                raise
            delay = 1.5 * attempt
            print(f"[native] AppleScript 暂不可用，{delay:.1f}s 后重试 ({attempt}/{attempts})")
            time.sleep(delay)


def _wait_signup_home(ui: RoxyNativeUI, *, country: str, timeout: float = 45.0) -> None:
    """Wait for signup URL and page render (non-empty title)."""
    ui.activate_tab_with_url("signup.live.com/signup")
    _ui_with_retry(ui, lambda: ui.wait_for_url(("signup.live.com/signup",), timeout=timeout))
    deadline = time.monotonic() + min(timeout, 30.0)
    last_title = ""
    while time.monotonic() < deadline:
        last_title = _ui_with_retry(ui, lambda: ui.title())
        low = last_title.lower()
        if last_title and (
            "create your microsoft account" in low
            or ("microsoft" in low and "account" in low)
        ):
            return
        if last_title and ("hisob" in low or "qaydnoma" in low):
            mkt, _ = locale_for_country(country)
            raise RegistrationError(
                f"注册页仍为非英文 locale title={last_title!r}，"
                f"请确认 Roxy fingerInfo language={mkt}"
            )
        time.sleep(1.0)
    if not last_title:
        print(f"[native] 注册页 title 仍为空，等待渲染后尝试继续 url={ui.url()}")
        time.sleep(4.0)
        return
    mkt, _ = locale_for_country(country)
    print(
        f"[native] ⚠ 注册页 title 未匹配英文模板 title={last_title!r} "
        f"expected mkt={mkt}，继续尝试"
    )


def _wait_signup_title(ui: RoxyNativeUI, *titles: str, timeout: float = 20.0) -> str:
    """Wait for one of the signup step titles without losing the last state.

    Native clicks can be delivered before React has committed the form state;
    callers use this helper to verify the transition and safely retry with a
    keyboard submit when it did not happen.
    """
    deadline = time.monotonic() + timeout
    last = ""
    needles = tuple(value.lower() for value in titles)
    while time.monotonic() < deadline:
        ui.activate_tab_with_url("signup.live.com/signup")
        last = ui.title()
        if any(needle in last.lower() for needle in needles):
            return last
        time.sleep(0.4)
    raise NativeUIError(
        f"timed out waiting for title {needles!r}; last={last!r}, url={ui.url()!r}"
    )


def _submit_password(ui: RoxyNativeUI, password: str, *, step_delay: float) -> None:
    """Fill and submit the password step, tolerating native click races."""
    # In the current 935x768 Roxy viewport the password input is centered
    # around y=445 and its Next button around y=556.  The old y=412/y=480
    # pair landed on the validation text and never entered the password.
    field_points = ((460, 445), (460, 430), (460, 412))
    for submit_attempt in range(1, 4):
        _dismiss_save_password(ui)
        field_x, field_y = field_points[min(submit_attempt - 1, len(field_points) - 1)]
        ui.click_relative(field_x, field_y)
        ui.shortcut("a")
        ui.type_text(password)
        # Chromium may open its native "Save password?" prompt only after
        # the first real password input.  If it is left open, the following
        # coordinate click is swallowed by that prompt instead of Next.
        _dismiss_save_password(ui)
        # The button position is stable in the reference viewport, but a
        # native click can occasionally land before the page's event handler
        # is attached.  Verify the title before trying the keyboard fallback.
        ui.click_relative(465, 556)
        time.sleep(step_delay)
        try:
            _wait_signup_title(ui, "Add some details", timeout=8)
            return
        except NativeUIError:
            if _is_registration_blocked(ui):
                raise RegistrationBlocked(f"Microsoft 拦截: {ui.title()}")
            # Re-focus the password control before the keyboard fallback.  A
            # successful native click may have focused the button itself,
            # while a missed click leaves focus in the field; normalizing the
            # focus makes Tab/Return deterministic in both cases.
            ui.click_relative(field_x, field_y)
            ui.shortcut("a")
            ui.type_text(password)
            _dismiss_save_password(ui)
            ui.press_many(("Tab", "Return"))
            try:
                _wait_signup_title(ui, "Add some details", timeout=10)
                return
            except NativeUIError:
                if submit_attempt == 3:
                    raise RegistrationError(
                        f"密码提交未进入生日页 title={ui.title()!r}"
                    ) from None
                print(f"[native] 密码提交未跳转，重试 ({submit_attempt}/3)")
                time.sleep(0.8)


def _normalize_birth_year(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        value = "1988"
    if not value.isdigit() or len(value) != 4:
        raise RegistrationError(f"birth-year 无效: {raw!r}（需要四位年份，默认 1988）")
    return value


_BIRTH_COUNTRY_NAMES = {
    "US": "United States",
    "GB": "United Kingdom",
    "CA": "Canada",
    "DE": "Germany",
    "FR": "France",
    "SG": "Singapore",
    "PH": "Philippines",
    "AU": "Australia",
}

_BIRTH_MONTH_INDEX = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _birth_month_index(month: str) -> int:
    key = (month or "August").strip().lower()
    idx = _BIRTH_MONTH_INDEX.get(key)
    if idx is None:
        raise RegistrationError(f"birth-month 无效: {month!r}（例如 August）")
    return idx


def _dismiss_open_dropdown(ui: RoxyNativeUI) -> None:
    ui.ensure_signup_tab()
    ui.press("Escape")
    time.sleep(0.12)


def _prepare_birthday_page(ui: RoxyNativeUI) -> None:
    closed = ui.close_about_blank_tabs()
    if closed:
        print(f"[native] 已关闭 {closed} 个 about:blank 标签")
    ui.ensure_signup_tab()
    _dismiss_save_password(ui)


def _submit_signup_tail_via_cdp(
    roxy: RoxyBrowserClient,
    profile_id: str,
    *,
    country: str,
    birth_day: str,
    birth_month: str,
    birth_year: str,
    first_name: str,
    last_name: str,
    keep_open: bool,
) -> str:
    """Birthday + name + create account via CDP (Fluent UI comboboxes)."""
    from roxy_register import (
        _choose_combobox,
        _click_next,
        _is_recovery_step_url,
        _visible,
        _wait_for_captcha_or_signup_result,
    )

    year_value = _normalize_birth_year(birth_year)
    cc = (country or "US").strip().upper()
    print(
        f"[native] 生日+姓名 CDP 填写 country={cc} day={birth_day} "
        f"month={birth_month} year={year_value} name={first_name} {last_name}"
    )
    with roxy_cdp_session(
        profile_id,
        client=roxy,
        close_on_exit=not keep_open,
    ) as (_, context):
        pages = [p for p in context.pages if "signup.live.com" in (p.url or "").lower()]
        page = pages[-1] if pages else context.pages[-1]
        page.bring_to_front()
        country_name = _BIRTH_COUNTRY_NAMES.get(cc, "")
        if country_name and cc != "US":
            if page.get_by_role("combobox", name="Country/Region", exact=True).count():
                _choose_combobox(page, "Country/Region", country_name)
        _choose_combobox(page, "Birth month", birth_month)
        _choose_combobox(page, "Birth day", str(birth_day))
        year = _visible(
            page,
            [
                'input[name*="BirthYear"]',
                'input[aria-label*="Birth year"]',
                'input[placeholder="Year"]',
                'input[type="number"]',
            ],
            timeout=15_000,
        )
        year.fill(year_value)
        _click_next(page)
        page.wait_for_function(
            "() => document.title.toLowerCase().includes('add your name')",
            timeout=20_000,
        )
        first_box = page.get_by_role("textbox", name="First name", exact=True)
        first_box.wait_for(state="visible", timeout=30_000)
        first_box.fill(first_name)
        last_box = page.get_by_role("textbox", name="Last name", exact=True)
        last_box.wait_for(state="visible", timeout=30_000)
        last_box.fill(last_name)
        print("[native] CDP 提交创建账号（Next）…")
        _click_next(page)
        try:
            state = _wait_for_captcha_or_signup_result(page, timeout=90_000)
        except RuntimeError as exc:
            raise RegistrationError(str(exc)) from exc
        if _is_recovery_step_url(page.url):
            state = "proofs"
        print(f"[native] 提交后页面状态: {state}")
        return state
    # CDP disconnect can briefly freeze Roxy AppleScript; pause before native CAPTCHA.
    time.sleep(2.5)


# 把 DOM 元素中心换算成屏幕绝对坐标。
#   x: screenX + 左右边框/2 + rect 中心
#   y: screenY + (outerHeight - innerHeight)（标签栏+地址栏，固定高度）+ rect 中心
# 固定高度这一项正是 926x768 等比换算算错的地方。
_SCREEN_POINT_JS = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return null;
  return {
    x: window.screenX + (window.outerWidth - window.innerWidth) / 2 + r.left + r.width / 2,
    y: window.screenY + (window.outerHeight - window.innerHeight) + r.top + r.height / 2,
    text: (el.textContent || el.value || '').trim().slice(0, 40),
  };
}
"""


def _screen_point(page: Any, selectors: Iterable[str]) -> tuple[int, int, str, str]:
    """返回首个可见元素的 (屏幕x, 屏幕y, 命中selector, 当前文本)。"""
    tried: list[str] = []
    for sel in selectors:
        tried.append(sel)
        try:
            pt = page.evaluate(_SCREEN_POINT_JS, sel)
        except Exception:  # noqa: BLE001
            continue
        if pt:
            return int(round(pt["x"])), int(round(pt["y"])), sel, str(pt.get("text") or "")
    raise RegistrationError(f"DOM 未找到可见元素，selectors={tried}")


def _native_pick_combobox(
    ui: RoxyNativeUI,
    page: Any,
    selectors: Iterable[str],
    value: str,
    *,
    label: str,
    option_index: int = 0,
) -> None:
    """DOM 定位 + 原生键鼠选中 Fluent 下拉项，并校验结果。

    Fluent 的 Month/Day 不是原生 ``<select>``，硬编码坐标点不中（实测 1000x1000
    窗口下必偏）。这里坐标现从 DOM 取，输入仍走真实 CGEvent；选完读回按钮文本
    校验，typeahead 没中就退回「方向键 N 次 + 回车」。
    """
    selectors = list(selectors)
    value = str(value).strip()
    for attempt in (1, 2, 3):
        x, y, sel, before = _screen_point(page, selectors)
        ui.click_absolute(x, y)
        time.sleep(0.35)
        if attempt < 3:
            ui.type_text(value if value.isdigit() else value[:3])
            time.sleep(0.25)
            ui.press("Return")
        else:
            # typeahead 失败兜底：列表已展开，方向键走到目标项
            for _ in range(max(1, option_index)):
                ui.press("Down")
                time.sleep(0.04)
            ui.press("Return")
        time.sleep(0.35)
        _, _, _, after = _screen_point(page, selectors)
        if value.lower()[:3] in after.lower():
            print(f"[native] {label}={after!r} ✓ (DOM坐标 {x},{y} sel={sel})")
            return
        print(
            f"[native] {label} 选中失败 attempt={attempt} "
            f"期望~{value!r} 实际={after!r}（点击 {x},{y}）"
        )
        ui.press("Escape")
        time.sleep(0.2)
    raise RegistrationError(f"{label} 无法选中 {value!r}（Fluent 下拉框）")


def _submit_birthday_native_dom(
    ui: RoxyNativeUI,
    page: Any,
    *,
    country: str,
    birth_day: str,
    birth_month: str,
    birth_year: str,
) -> None:
    """生日页：DOM 取坐标 + 全程原生键鼠（不用 CDP 注入输入）。"""
    cc = (country or "US").strip().upper()
    year_value = _normalize_birth_year(birth_year)
    month_idx = _MONTH_ORDER.index(birth_month) + 1 if birth_month in _MONTH_ORDER else 1
    print(
        f"[native] 生日页 DOM坐标+原生输入 country={cc} "
        f"month={birth_month} day={birth_day} year={year_value}"
    )

    country_name = _BIRTH_COUNTRY_NAMES.get(cc, "")
    if country_name and cc != "US":
        _native_pick_combobox(
            ui, page, ["#countryDropdownId", '[aria-label*="Country"]'],
            country_name, label="国家",
        )
    _native_pick_combobox(
        ui, page, ["#BirthMonthDropdown", '[aria-label*="Birth month"]'],
        birth_month, label="出生月", option_index=month_idx,
    )
    _native_pick_combobox(
        ui, page, ["#BirthDayDropdown", '[aria-label*="Birth day"]'],
        str(birth_day), label="出生日", option_index=int(birth_day),
    )

    x, y, sel, _ = _screen_point(page, [
        'input[name*="BirthYear"]', 'input[placeholder="Year"]',
        'input[aria-label*="Birth year"]', 'input[type="number"]',
    ])
    ui.click_absolute(x, y)
    time.sleep(0.2)
    ui.shortcut("a")
    ui.type_text(year_value)
    time.sleep(0.3)
    print(f"[native] 出生年={year_value} (DOM坐标 {x},{y} sel={sel})")


def _fill_birth_combobox(ui: RoxyNativeUI, x: int, y: int, value: str) -> None:
    """Select Fluent combobox option without Cmd+A (avoids selecting whole page)."""
    value = str(value).strip()
    ui.ensure_signup_tab()
    ui.click_relative(x, y)
    time.sleep(0.25)
    if value.isdigit():
        ui.type_text(value)
        ui.press("Return")
    else:
        ui.type_text(value[:3])
        time.sleep(0.15)
        ui.press("Return")
    time.sleep(0.35)


def _fill_birth_year_field(ui: RoxyNativeUI, x: int, y: int, year: str) -> None:
    year = _normalize_birth_year(year)
    ui.ensure_signup_tab()
    ui.click_relative(x, y)
    time.sleep(0.25)
    ui.shortcut("a")
    ui.type_text(year)
    time.sleep(0.35)


def _submit_birthday(
    ui: RoxyNativeUI,
    *,
    country: str,
    birth_day: str,
    birth_month: str,
    birth_year: str,
    step_delay: float,
) -> None:
    """Fill country + birthdate; year is on its own row below day/month."""
    cc = (country or "US").strip().upper()
    year_value = _normalize_birth_year(birth_year)
    print(
        f"[native] 生日填写 country={cc} day={birth_day} month={birth_month} year={year_value}"
    )
    country_y, combo_y, year_y, next_y = 310, 378, 452, 518
    left_x, right_x = 347, 463
    year_x = 347
    country_name = _BIRTH_COUNTRY_NAMES.get(cc, "")

    for submit_attempt in range(1, 4):
        _prepare_birthday_page(ui)
        if country_name and cc != "US":
            ui.click_relative(460, country_y)
            time.sleep(0.3)
            ui.shortcut("a")
            ui.type_text(country_name)
            ui.press("Return")
            time.sleep(0.4)
        if cc == "US":
            _fill_birth_combobox(ui, left_x, combo_y, birth_month)
            _fill_birth_combobox(ui, right_x, combo_y, birth_day)
        else:
            _fill_birth_combobox(ui, left_x, combo_y, birth_day)
            _fill_birth_combobox(ui, right_x, combo_y, birth_month)
        _fill_birth_year_field(ui, year_x, year_y, year_value)
        _dismiss_save_password(ui)
        ui.click_relative(465, next_y)
        time.sleep(step_delay)
        try:
            _wait_signup_title(ui, "Add your name", timeout=10)
            return
        except NativeUIError:
            if _is_registration_blocked(ui):
                raise RegistrationBlocked(f"Microsoft 拦截: {ui.title()}")
            ui.click_relative(year_x, year_y)
            _fill_birth_year_field(ui, year_x, year_y, year_value)
            ui.press_many(("Tab", "Return"))
            try:
                _wait_signup_title(ui, "Add your name", timeout=12)
                return
            except NativeUIError:
                if submit_attempt == 3:
                    raise RegistrationError(
                        f"生日提交未进入姓名页 title={ui.title()!r} "
                        f"(year={year_value!r})"
                    ) from None
                print(f"[native] 生日提交未跳转，重试 ({submit_attempt}/3)")
                time.sleep(0.8)


def _submit_email(
    ui: RoxyNativeUI,
    email: str,
    *,
    step_delay: float,
) -> None:
    """Fill and submit the email step; coordinates match 926x768 Roxy viewport."""
    field_points = ((460, 412), (460, 380), (460, 395))
    next_points = ((465, 480), (465, 490), (465, 505))
    for submit_attempt in range(1, 4):
        _dismiss_save_password(ui)
        ui.activate_tab_with_url("signup.live.com/signup")
        time.sleep(0.6)
        field_x, field_y = field_points[min(submit_attempt - 1, len(field_points) - 1)]
        next_x, next_y = next_points[min(submit_attempt - 1, len(next_points) - 1)]
        ui.click_relative(field_x, field_y)
        time.sleep(0.25)
        ui.shortcut("a")
        # 第 1 轮按「前缀 + 域名下拉框」变体试；第 1 轮已失败说明大概率是
        # 「要求完整地址」变体，后续轮次直接填全地址，别再浪费轮次。
        ui.type_text(_email_local_for_signup(email) if submit_attempt == 1 else email)
        time.sleep(0.35)
        _dismiss_save_password(ui)
        ui.click_relative(next_x, next_y)
        time.sleep(step_delay)
        # Some locales briefly expose a second account-name page on the same title.
        if "create your microsoft account" in ui.title().lower():
            ui.press_many(("Return", "Tab", "Return"))
            time.sleep(step_delay)
        try:
            _wait_signup_title(ui, "Create your password", timeout=10)
            return
        except NativeUIError:
            if _is_registration_blocked(ui):
                raise RegistrationBlocked(f"Microsoft 拦截: {ui.title()}")
            # 微软对邮箱页做 A/B：一种是「前缀 + @outlook.com 下拉框」，
            # 另一种要求填完整地址（只填前缀会报
            # "Enter your email address in the format: someone@example.com" 卡死）。
            # 首次按前缀试，未跳转则改用完整地址重试，覆盖两种变体。
            ui.click_relative(field_x, field_y)
            ui.shortcut("a")
            ui.type_text(email)
            ui.press_many(("Tab", "Return"))
            time.sleep(step_delay)
            try:
                _wait_signup_title(ui, "Create your password", timeout=12)
                return
            except NativeUIError:
                if _is_registration_blocked(ui):
                    raise RegistrationBlocked(f"Microsoft 拦截: {ui.title()}")
                if submit_attempt == 3:
                    raise RegistrationError(
                        f"未能进入密码页 title={ui.title()!r}"
                    ) from None
                print(f"[native] 邮箱提交未跳转，重试 ({submit_attempt}/3)")
                time.sleep(0.8)


def _run_signup_form(
    ui: RoxyNativeUI,
    *,
    email: str,
    password: str,
    args: argparse.Namespace,
    auto_mode: bool,
    first_name: str,
    last_name: str,
    roxy: RoxyBrowserClient | None = None,
    profile_id: str = "",
) -> None:
    signup_url = NATIVE_SIGNUP_URL
    print(
        f"[native] 打开注册页 proxy_country={args.country} "
        f"ui=en-US → {signup_url}"
    )
    ui._maybe_activate()
    ui.navigate("https://www.bing.com/")
    time.sleep(2.5)
    ui.open_signup_url(signup_url)
    for signup_attempt in range(1, 4):
        try:
            _wait_signup_home(ui, country=args.country, timeout=40)
            break
        except NativeUIError:
            if signup_attempt == 3:
                raise RegistrationError("注册首页未就绪") from None
            print(f"[native] 注册首页未就绪，原标签重载 ({signup_attempt}/3)")
            ui.open_signup_url(signup_url)
            time.sleep(2.5)
    _settle_signup_home_page(ui, signup_url=signup_url, auto=args.auto_steps)
    time.sleep(0.5)

    _submit_email(ui, email, step_delay=args.step_delay)
    _checkpoint(ui, "密码页", auto=args.auto_steps)

    _submit_password(ui, password, step_delay=args.step_delay)
    # The save-password bubble is often shown asynchronously a moment after
    # the password transition.  Dismiss it before touching the birth form so
    # it cannot steal focus from the native Tab sequence.
    time.sleep(0.8)
    _dismiss_save_password(ui)
    _checkpoint(ui, "生日页", auto=args.auto_steps)
    post_submit_state: str | None = None
    if getattr(args, "birthday_via_cdp", True) and roxy is not None and profile_id:
        print("[native] 生日+姓名改用 CDP（邮箱/密码仍为 CGEvent 原生键盘）")
        post_submit_state = _submit_signup_tail_via_cdp(
            roxy,
            profile_id,
            country=args.country,
            birth_day=args.birth_day,
            birth_month=args.birth_month,
            birth_year=args.birth_year,
            first_name=first_name,
            last_name=last_name,
            keep_open=args.keep_open,
        )
        _ui_with_retry(ui, lambda: ui.ensure_signup_tab())
    else:
        _submit_birthday(
            ui,
            country=args.country,
            birth_day=args.birth_day,
            birth_month=args.birth_month,
            birth_year=args.birth_year,
            step_delay=args.step_delay,
        )
        _checkpoint(ui, "姓名页", auto=args.auto_steps)
        _dismiss_save_password(ui)
        ui.click_relative(460, 365)
        ui.type_text(first_name)
        ui.click_relative(460, 425)
        ui.type_text(last_name)

    if not args.full:
        print("\n[native] 已停在最终创建账号之前。")
        print("请在 Roxy 中检查姓名和营销复选框；需要继续时由你亲自点击 Next。")
        print(f"测试邮箱: {email}")
        print(f"测试密码: {password}")
        return

    if post_submit_state is None:
        print("\n[native] 即将点击最终 Next 并正式创建 Microsoft 账号。")
        if not auto_mode:
            input("确认创建该账号后按 Enter 继续：")
            ui._maybe_activate()
            time.sleep(0.3)
        deadline = time.monotonic() + 45
        resubmit_at = time.monotonic() + 6
        _dismiss_save_password(ui)
        ui.click_relative(465, 615)
        time.sleep(args.step_delay)
        while time.monotonic() < deadline:
            _ui_with_retry(
                ui,
                lambda: ui.activate_tab_with_url("signup.live.com/signup") or True,
            )
            if _is_registration_blocked(ui):
                raise RegistrationBlocked(f"Microsoft 拦截了账号创建: {ui.title()}")
            title = _ui_with_retry(ui, lambda: ui.title())
            url = _ui_with_retry(ui, lambda: ui.url())
            lowered_title = title.lower()
            if "prove you're human" in lowered_title:
                post_submit_state = "captcha"
                break
            if _is_proofs_step_url_or_title(url=url, title=title):
                post_submit_state = "proofs"
                break
            if time.monotonic() >= resubmit_at and (
                "add your name" in lowered_title
                or "create your microsoft account" in lowered_title
            ):
                ui._maybe_activate()
                ui.press_many(("Tab", "Return"))
                resubmit_at = time.monotonic() + 8
            time.sleep(0.5)
        else:
            raise RegistrationError(
                f"姓名提交后未进入 CAPTCHA/proofs title={ui.title()!r}"
            )

    if _is_registration_blocked(ui):
        raise RegistrationBlocked(f"Microsoft 拦截了账号创建: {ui.title()}")

    if post_submit_state == "proofs" or _is_proofs_step(ui):
        print("[native] 已进入恢复邮箱验证（proofs 页）")
    elif post_submit_state == "captcha" or "prove you're human" in ui.title().lower():
        print("[native] 已进入 CAPTCHA，自动执行 accessible 按压。")
        if not auto_mode:
            input("确认执行可访问 CAPTCHA 后按 Enter 继续：")
        ui._maybe_activate()
        time.sleep(0.5)
        _run_accessible_captcha(ui)
    else:
        _wait_for_post_captcha(ui, timeout=90)
    print("[native] CAPTCHA/proofs 就绪，开始 CF 恢复邮箱 + OAuth")


def _finish_oauth_and_save(
    *,
    roxy: RoxyBrowserClient,
    profile_id: str,
    ui: RoxyNativeUI,
    email: str,
    password: str,
    recovery_email: str,
    auto_mode: bool,
    keep_open: bool,
    batch_label: str,
) -> RegisterResult:
    from roxy_register import (
        GRAPH_SCOPE,
        MAIL_CLIENT_ID,
        _exchange,
        _finish_account_setup,
        _oauth,
        _open_outlook,
        _proofs,
    )

    ui._maybe_activate()
    time.sleep(0.5)
    if not _is_proofs_step(ui):
        print("[native] 等待 proofs 页…")
        _wait_for_post_captcha(ui, timeout=60)

    with roxy_cdp_session(
        profile_id,
        client=roxy,
        close_on_exit=not keep_open,
    ) as (_, context):
        candidates = [
            page for page in context.pages
            if (
                "account.live.com/proofs" in page.url.lower()
                or "account.live.com/interrupt/credentialaction" in page.url.lower()
            )
        ]
        page = candidates[-1] if candidates else context.pages[-1]
        page.bring_to_front()
        print(f"[native] proofs 页 CDP url={page.url[:100]}")
        _proofs(page, recovery_email)
        _finish_account_setup(page, native_ui=ui, auto=auto_mode, timeout=180_000)
        _open_outlook(page, native_ui=ui, auto=auto_mode)
        oauth_page = context.new_page()
        _, redirect_url = _oauth(oauth_page, native_ui=ui, auto=auto_mode)

    code = (urllib.parse.parse_qs(urllib.parse.urlsplit(redirect_url).query).get("code") or [""])[0]
    if not code:
        raise RegistrationError("OAuth 回调缺少 code")
    token = _exchange(code)
    result = RegisterResult(
        success=True,
        email=email,
        password=password,
        redirect_url=redirect_url,
        refresh_token=token["refresh_token"],
        client_id=MAIL_CLIENT_ID,
        recovery_email=recovery_email,
        recovery_password="cf_domain",
        extra={
            "route": "roxy_native_ui",
            "proofs_method": "cf_domain",
            "proofs_satisfied": "yes",
            "scope": GRAPH_SCOPE,
        },
    )
    saved = save_register_result(result, str(PROJECT_DIR / "accounts"), batch_label=batch_label)
    print(f"[native] 注册成功并已落库: {saved}")
    print(f"邮箱: {email}\n密码: {password}")
    return result


def _register_one(
    *,
    args: argparse.Namespace,
    roxy: RoxyBrowserClient,
    profile_id: str,
    ui: RoxyNativeUI,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
    auto_mode: bool,
    batch_label: str,
) -> RegisterResult | None:
    recovery_email = args.recovery_email or allocate_address(CFDomainMailClient())
    print(f"[native] email={email} name={first_name} {last_name} recovery={recovery_email}")
    _run_signup_form(
        ui,
        email=email,
        password=password,
        args=args,
        auto_mode=auto_mode,
        first_name=first_name,
        last_name=last_name,
        roxy=roxy,
        profile_id=profile_id,
    )
    if not args.full:
        return None
    return _finish_oauth_and_save(
        roxy=roxy,
        profile_id=profile_id,
        ui=ui,
        email=email,
        password=password,
        recovery_email=recovery_email,
        auto_mode=auto_mode,
        keep_open=args.keep_open,
        batch_label=batch_label,
    )


def _load_proxy_lines(path: str) -> list[str]:
    lines: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _parse_countries(raw: str) -> list[str]:
    out = [part.strip().upper() for part in (raw or "").split(",") if part.strip()]
    return out or ["US"]


def _country_for_attempt(
    *,
    attempt_index: int,
    proxy_line: str,
    countries: list[str],
    default_country: str,
) -> str:
    from_proxy = infer_zone_country(proxy_line)
    if from_proxy and from_proxy != "GL":
        return from_proxy
    if len(countries) == 1:
        return countries[0]
    return countries[(attempt_index - 1) % len(countries)]


def _apply_attempt_proxy(
    roxy: RoxyBrowserClient,
    profile_id: str,
    proxy_line: str,
    country: str,
) -> None:
    if not proxy_line:
        return
    effective = rewrite_ipwo_zone_country(proxy_line, country)
    if effective != proxy_line:
        print(f"[native] IPWO zone GLOBAL → {country.upper()}（出口与 locale 对齐）")
        proxy_line = effective
    masked = proxy_line.split(":")[0] if ":" in proxy_line else proxy_line[:24]
    sid_m = re.search(r"sid_(\d+)", proxy_line)
    sid_hint = f" sid={sid_m.group(1)}" if sid_m else ""
    ok, detail = preflight_proxy(proxy_line, timeout=12)
    ip_hint = detail.strip().replace("\n", " ")[:80]
    print(
        f"[native] 切换 Roxy 代理 → {masked}… country={country}{sid_hint} "
        f"预检={'OK' if ok else 'FAIL'} {ip_hint}"
    )
    if not ok:
        raise RegistrationError(f"代理预检失败: {detail[:120]}")
    _retry_roxy(
        "更新 profile 代理",
        lambda: roxy.update_profile_proxy_native(
            profile_id, proxy_line, country=country
        ),
    )
    time.sleep(2.0)


def _print_batch_summary(stats: BatchStats) -> None:
    print("\n" + "=" * 48)
    print(
        f"[batch] 目标 {stats.target}，"
        f"成功 {len(stats.successes)}，失败 {len(stats.failures)}"
    )
    for result in stats.successes:
        print(f"  ✓ {result.email}")
    for email, reason in stats.failures:
        print(f"  ✗ {email or '(未生成)'}: {reason[:100]}")
    print("=" * 48)


def main() -> int:
    parser = argparse.ArgumentParser(description="Roxy + macOS 原生键盘 Outlook 注册测试")
    profiles = parser.add_mutually_exclusive_group(required=True)
    profiles.add_argument("--profile-id")
    profiles.add_argument("--profile-name")
    parser.add_argument("--email", help="留空则生成随机 @outlook.com 地址（批量时忽略）")
    parser.add_argument("--password", help="留空则生成随机强密码（批量时忽略）")
    parser.add_argument("-n", "--count", type=int, default=1, help="目标成功注册数量，默认 1")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=0,
        help="最多尝试次数（含失败）；默认 count×3，至少 count",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=8.0,
        help="失败后清 Cookie 重试前的等待秒数，默认 8",
    )
    parser.add_argument(
        "--blocked-delay",
        type=float,
        default=0.0,
        help="风控 blocked 后的额外等待秒数，默认 0（立即换代理重试）",
    )
    parser.add_argument(
        "--rotate-fingerprint",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="每轮尝试轮换到下一个 Roxy profile（默认开启；可用 --no-rotate-fingerprint 关闭）",
    )
    parser.add_argument("--first-name", default="Adrian")
    parser.add_argument("--last-name", default="Silva")
    parser.add_argument("--birth-day", default="23")
    parser.add_argument("--birth-month", default="August")
    parser.add_argument("--birth-year", default="1988")
    parser.add_argument(
        "--birthday-via-cdp",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="生日页用 CDP 填 Fluent 下拉框（默认开；邮箱/密码仍原生键盘）",
    )
    parser.add_argument("--country", default="US", help="默认国家；有 --countries/--proxies-file 时每轮可轮换")
    parser.add_argument(
        "--countries",
        help="轮换国家列表，逗号分隔，如 US,PH,SG,CA（配合 proxies-file 每轮换 IP+国家）",
    )
    parser.add_argument("--proxy", help="单条代理 host:port:user:pass；批量时可用 --proxies-file")
    parser.add_argument(
        "--proxies-file",
        help="代理列表文件，每行一条；每轮尝试轮换写入 Roxy profile（一号一 sid/IP）",
    )
    parser.add_argument("--keep-cookies", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--auto-steps", action="store_true", help="不在页面阶段间等待人工确认")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="全自动注册：等同 --full --auto-steps，且跳过创建/CAPTCHA/OAuth 的 Enter 确认",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="继续创建账号、CAPTCHA、CF proof、Outlook 与 OAuth/token",
    )
    parser.add_argument("--recovery-email", help="完整流程的恢复邮箱；留空则从项目 CF Worker 分配")
    parser.add_argument(
        "--resume-oauth",
        action="store_true",
        help="跳过注册表单，从当前 proofs 页继续 CF 绑邮 + OAuth 落库（需 --email --password --full）",
    )
    parser.add_argument("--step-delay", type=float, default=1.4)
    parser.add_argument(
        "--no-steal-focus",
        action="store_true",
        help="不抢前台焦点；Roxy 需已在前台时键盘/点击才生效",
    )
    parser.add_argument("--batch-label", default="roxy-native", help="落库 batch_label")
    args = parser.parse_args()
    args.birth_year = _normalize_birth_year(args.birth_year)
    if args.auto:
        args.full = True
        args.auto_steps = True
    auto_mode = args.auto or (args.full and args.auto_steps)

    if args.resume_oauth:
        if not args.full:
            raise SystemExit("--resume-oauth 需要 --full（或 --auto）")
        if not args.email or not args.password:
            raise SystemExit("--resume-oauth 需要 --email 与 --password")
        _load_roxy_settings_from_account_manager()
        roxy = RoxyBrowserClient()
        if not roxy.health():
            raise SystemExit(f"Roxy API 不可用: {roxy.last_error}")
        profile_id = _resolve_profile(roxy, args.profile_id or "", args.profile_name or "")
        recovery_email = args.recovery_email or allocate_address(CFDomainMailClient())
        ui = RoxyNativeUI(steal_focus=not args.no_steal_focus)
        print(
            f"[native] 续跑 OAuth：email={args.email} recovery={recovery_email} "
            f"（Roxy 窗口应停在 proofs 页）"
        )
        _finish_oauth_and_save(
            roxy=roxy,
            profile_id=profile_id,
            ui=ui,
            email=args.email.strip(),
            password=args.password,
            recovery_email=recovery_email,
            auto_mode=auto_mode,
            keep_open=args.keep_open,
            batch_label=args.batch_label,
        )
        return 0

    if args.count < 1:
        raise SystemExit("--count 必须 >= 1")
    if args.count > 1 and (args.email or args.password):
        print("[batch] 批量模式下忽略 --email/--password，每轮自动生成")
    max_attempts = args.max_attempts or max(args.count * 3, args.count)
    countries = _parse_countries(args.countries or args.country)
    proxy_lines: list[str] = []
    if args.proxies_file:
        proxy_path = Path(args.proxies_file).expanduser()
        if not proxy_path.is_file():
            raise SystemExit(f"代理文件不存在: {proxy_path}")
        proxy_lines = _load_proxy_lines(str(proxy_path))
        if not proxy_lines:
            raise SystemExit(f"代理文件为空: {proxy_path}")
        print(f"[batch] 已加载 {len(proxy_lines)} 条代理，countries={','.join(countries)}")
    elif args.proxy:
        proxy_lines = [args.proxy.strip()]

    if sys.platform != "darwin":
        raise SystemExit("原生 UI 模式目前仅支持 macOS")

    _load_roxy_settings_from_account_manager()
    roxy = RoxyBrowserClient()
    if not roxy.health():
        raise SystemExit(f"Roxy API 不可用: {roxy.last_error}")
    profile_id = _resolve_profile(roxy, args.profile_id or "", args.profile_name or "")

    profile_rotation = [profile_id]
    if args.rotate_fingerprint:
        # 轮换完整 Roxy profile 比在同一 profile 上拼接 fingerInfo 更可靠：
        # 每个 profile 自带一致的 Canvas/WebGL/硬件参数，避免指纹半更新。
        profile_rotation.extend(
            profile.id for profile in roxy.list_profiles() if profile.id != profile_id
        )
        print(f"[batch] 指纹轮换已启用，profiles={len(profile_rotation)}")

    for other_profile in roxy.list_profiles():
        if other_profile.id != profile_id:
            _retry_roxy(
                f"关闭非目标 profile {other_profile.name}",
                lambda other_id=other_profile.id: roxy.close(other_id),
            )
    print("[native] 已关闭其他 Roxy profile，目标窗口独占原生输入")
    print("[native] 注册表单走 macOS CGEvent 原生键盘（RoxyNativeUI）；proofs/OAuth 仍用 CDP")
    # 没有辅助功能授权时 CGEvent 按键被系统静默丢弃：页面会一直停在邮箱页，
    # 报“未能进入密码页”，看起来像风控，实际是权限问题。必须提前拦住。
    if not accessibility_trusted():
        app = responsible_app_hint()
        raise SystemExit(
            "[native] 缺少 macOS「辅助功能」授权，原生键盘无法输入（按键会被静默丢弃）。\n"
            f"  请到 系统设置 → 隐私与安全性 → 辅助功能，勾选 {app}，然后【完全退出并重开】该 App。\n"
            "  自检：python -c \"from service.browser.macos_native_ui import accessibility_trusted as t; print(t())\"\n"
            "  须打印 True 才能继续。"
        )
    if args.no_steal_focus:
        print("[native] --no-steal-focus：脚本不会 activate Roxy，请保持 Roxy 在前台")
    print(f"[batch] 目标成功 {args.count} 个，最多尝试 {max_attempts} 次")

    stats = BatchStats(target=args.count)
    ui = RoxyNativeUI(steal_focus=not args.no_steal_focus)
    attempt = 0
    consecutive_blocked = 0
    active_profile_id = ""

    while len(stats.successes) < args.count and attempt < max_attempts:
        attempt += 1
        attempt_profile_id = profile_rotation[(attempt - 1) % len(profile_rotation)]
        email = _gen_email(args.email if args.count == 1 else "")
        password = _gen_password(args.password if args.count == 1 else "")
        if args.count > 1:
            first_name, last_name = _random_identity()
        else:
            first_name, last_name = args.first_name, args.last_name
        proxy_line = proxy_lines[(attempt - 1) % len(proxy_lines)] if proxy_lines else ""
        attempt_country = _country_for_attempt(
            attempt_index=attempt,
            proxy_line=proxy_line,
            countries=countries,
            default_country=args.country,
        )
        args.country = attempt_country
        print(
            f"\n[batch] 第 {attempt}/{max_attempts} 次尝试，"
            f"已成功 {len(stats.successes)}/{args.count} → {email} "
            f"country={attempt_country} profile={attempt_profile_id[:8]}"
        )

        try:
            if active_profile_id and active_profile_id != attempt_profile_id:
                # Roxy can leave a previous profile window alive briefly;
                # close every rotated profile before opening the next one so
                # native input cannot land in a stale window.
                for old_profile_id in profile_rotation:
                    if old_profile_id != attempt_profile_id:
                        _retry_roxy(
                            "关闭上一轮 profile",
                            lambda old_id=old_profile_id: roxy.close(old_id),
                        )
                time.sleep(1.5)
            active_profile_id = attempt_profile_id
            if proxy_line:
                _apply_attempt_proxy(roxy, attempt_profile_id, proxy_line, attempt_country)
            if not args.keep_cookies or attempt > 1:
                _reset_profile(roxy, attempt_profile_id)
                if attempt == 1:
                    print("[native] 已清空 profile Cookie/缓存")
            elif attempt == 1:
                _retry_roxy("打开 profile", lambda: roxy.open(profile_id))
                time.sleep(2.0)

            result = _register_one(
                args=args,
                roxy=roxy,
                profile_id=attempt_profile_id,
                ui=ui,
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
                auto_mode=auto_mode,
                batch_label=args.batch_label,
            )
            if not args.full:
                return 0
            if result is not None:
                stats.successes.append(result)
                consecutive_blocked = 0
            print(f"[batch] ✓ 成功 {len(stats.successes)}/{args.count}")
            if len(stats.successes) < args.count and not args.keep_cookies:
                time.sleep(max(1.0, args.retry_delay / 2))

        except RegistrationBlocked as exc:
            consecutive_blocked += 1
            stats.failures.append((email, str(exc)))
            delay = max(0.0, args.blocked_delay)
            print(f"[batch] 风控拦截，立即换代理/指纹重试: {exc}")
            if consecutive_blocked >= 3:
                print(
                    "[batch] ⚠ 连续 3 次 blocked：注册表单逻辑未变，"
                    "通常是当前 Roxy 代理 IP/指纹已被微软标记。"
                    "建议换代理出口或等待 1-2 小时后再试。"
                )
            if delay:
                time.sleep(delay)
        except (NativeUIError, RegistrationError, RuntimeError) as exc:
            stats.failures.append((email, str(exc)))
            print(f"[batch] 失败，{args.retry_delay:.0f}s 后重试: {exc}")
            time.sleep(args.retry_delay)

    if args.count > 1 or stats.failures:
        _print_batch_summary(stats)

    if not args.full:
        return 0
    if len(stats.successes) >= args.count:
        return 0
    raise SystemExit(
        f"未达目标：成功 {len(stats.successes)}/{args.count}，"
        f"已尝试 {attempt}/{max_attempts}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
