#!/usr/bin/env python3
"""Capture polished Web UI screenshots for README (demo data + secret masking)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8890"
OUT = Path(__file__).resolve().parent.parent / "assets" / "screenshots"

# 全局脱敏：代理地址 / captcha / token / 真实邮箱 一律不露出
SCRUB_JS = r"""
() => {
  const hideProxyField = () => {
    const pool = document.getElementById('useProxyPool');
    const proxy = document.getElementById('proxy');
    if (pool) pool.checked = true;
    if (proxy) {
      proxy.value = '';
      proxy.placeholder = '已启用代理池，按账号自动分配';
      proxy.setAttribute('disabled', 'disabled');
      proxy.style.color = 'var(--fg-3)';
    }
    const key = document.getElementById('captchaKey');
    if (key) key.value = '••••••••••••••••';
  };
  hideProxyField();

  const PROXY_RE = /(?:gate\.|brd\.|kookeey|rapidproxy|brightdata|iproyal|ipwo|:\d{4}:)[^\s<]*/gi;
  const IP_RE = /\b(?:\d{1,3}\.){3}\d{1,3}\b/g;

  const scrubText = (t) => {
    if (!t || !t.trim()) return t;
    let s = t.replace(PROXY_RE, '••••');
    s = s.replace(IP_RE, '•••');
    return s;
  };

  document.querySelectorAll('input, textarea').forEach((el) => {
    if (el.id === 'proxy') return;
    if (el.type === 'password' || /key|captcha|token|proxy/i.test(el.id || '')) {
      el.value = '••••••••';
    }
  });

  document.querySelectorAll('td, th, .mono, code, p, span, div').forEach((el) => {
    if (el.children.length > 0) return;
    if (el.closest('#jobsBody, #statCards, #poolBody')) return;
    const raw = el.textContent || '';
    if (!raw.trim()) return;
    if (PROXY_RE.test(raw) || IP_RE.test(raw) || /----/.test(raw)) {
      if (el.closest('#proxyBody') && el.cellIndex === 4) {
        el.textContent = '已配置';
        return;
      }
      if (el.closest('#proxyBindBody')) {
        const col = el.cellIndex;
        if (col === 0) el.textContent = 'demo@outlook.com';
        else if (col === 2) el.textContent = '••••••••';
        else return;
      }
      el.textContent = scrubText(raw);
    }
  });
}
"""

INJECT_OVERVIEW = r"""
() => {
  const stats = { total: 128, with_token: 124, usable: 112, dead: 8, untested: 8, today_new: 12 };
  const items = [
    ['总数', stats.total, 'var(--fg)'],
    ['有Token', stats.with_token, 'var(--info)'],
    ['可用', stats.usable, 'var(--success)'],
    ['失活', stats.dead, 'var(--danger)'],
    ['未测', stats.untested, 'var(--fg-3)'],
    ['今日新增', stats.today_new, 'var(--brand)'],
  ];
  const el = document.getElementById('statCards');
  if (el) {
    el.innerHTML = items.map(([l, v, c]) =>
      `<div class="card stat"><div class="lbl">${l}</div><div class="num" style="color:${c}">${v}</div></div>`
    ).join('');
  }
  const jobs = [
    ['US-Residential-10x', '2026-08-24 14:32:08', 10, 2, 'graph_recovery', 'done', 9, 1],
    ['US-Residential-5x', '2026-08-24 11:05:22', 5, 1, 'graph_recovery', 'done', 5, 0],
    ['SG-Test-3x', '2026-08-23 22:18:41', 3, 1, 'login_exe', 'done', 3, 0],
    ['US-Night-8x', '2026-08-23 09:40:15', 8, 2, 'graph_recovery', 'done', 7, 1],
  ];
  const body = document.getElementById('jobsBody');
  if (body) {
    body.innerHTML = jobs.map(([label, ts, cnt, conc, mode, st, ok, fail]) => {
      const stcls = st === 'done' ? 'ok' : 'run';
      return `<tr>
        <td class="mono">${label}</td><td>${ts}</td><td>${cnt}</td><td>${conc}</td>
        <td>${mode}</td><td><span class="st ${stcls}">${st}</span></td>
        <td><span style="color:var(--success)">${ok}</span> / <span style="color:var(--danger)">${fail}</span></td>
      </tr>`;
    }).join('');
  }
  const nav = document.getElementById('navPoolCount');
  if (nav) nav.textContent = '128';
  const navP = document.getElementById('navProxyCount');
  if (navP) navP.textContent = '6';
  const foot = document.getElementById('footEngine');
  if (foot) foot.textContent = '批量:引擎 · dual:就绪 · 代理池:6';
}
"""

INJECT_REGISTER = r"""
() => {
  const logs = [
    ['14:32:01', 'INFO', '代理预检通过 · US'],
    ['14:32:03', 'INFO', 'PX solver 预加载完成 · press 模式'],
    ['14:32:08', 'INFO', '#1 CheckAvailableSigninName → ok'],
    ['14:32:14', 'INFO', '#1 risk/verify #1 → continuationToken'],
    ['14:32:19', 'INFO', '#1 captcha.run press → solved'],
    ['14:32:24', 'INFO', '#1 CreateAccount → 201'],
    ['14:32:31', 'INFO', '#1 proofs 绑定恢复邮箱 → ok'],
    ['14:32:38', 'INFO', '#1 OAuth refresh_token → Graph 可读信 ✓'],
    ['14:32:45', 'INFO', '#2 开始注册…'],
    ['14:32:52', 'INFO', '#2 CreateAccount → 201'],
  ];
  const c = document.getElementById('console');
  if (c) {
    c.innerHTML = logs.map(([ts, lv, msg]) =>
      `<div class="l-${lv}"><span class="ts">${ts}</span>${msg}</div>`
    ).join('');
  }
  const badge = document.getElementById('jobBadge');
  if (badge) badge.innerHTML = '<span class="badge brand">US-Residential-10x</span> <span class="badge neutral">真实 · 10个 · 并发2</span>';
  const live = document.getElementById('logLive');
  if (live) { live.style.display = 'inline'; live.textContent = '● 实时'; }
  const rows = [
    [1, '成功', 'kzmplqwerxty@outlook.com', '••••••••', '••••••••', ''],
    [2, '成功', 'bnhfgrtwsxpl@outlook.com', '••••••••', '••••••••', ''],
    [3, '进行中', '—', '—', '—', ''],
    [4, '等待中', '—', '—', '—', ''],
  ];
  const pb = document.getElementById('progressBody');
  if (pb) {
    const stMap = { '等待中': 'wait', '进行中': 'run', '成功': 'ok' };
    pb.innerHTML = rows.map(([i, st, em, pw, rt, err]) =>
      `<tr><td>${i}</td><td><span class="st ${stMap[st] || 'wait'}">${st}</span></td>
        <td class="mono">${em}</td><td>${pw}</td><td>${rt}</td>
        <td style="color:var(--danger);font-size:11px;">${err}</td></tr>`
    ).join('');
  }
  ['nTotal', 'nOk', 'nErr', 'nRun'].forEach((id, i) => {
    const el = document.getElementById(id);
    if (el) el.textContent = ['10', '2', '0', '1'][i];
  });
  const sum = document.getElementById('batchSummary');
  if (sum) {
    sum.className = 'msgbox show ok';
    sum.innerHTML = '<b>本批小结</b>　成功 2/10（失败 0）　本批耗时 <b>47s</b>　单号均耗 <b>23.5s</b><br><span style="color:var(--fg-3)">阶段平均：</span><span class="badge neutral">PX 6.2s</span> <span class="badge neutral">CreateAccount 8.1s</span> <span class="badge neutral">proofs 5.4s</span>';
  }
  const count = document.getElementById('count');
  if (count) count.value = '10';
  const conc = document.getElementById('concurrency');
  if (conc) conc.value = '2';
  const batch = document.getElementById('batchLabel');
  if (batch) batch.value = 'US-Residential-10x';
}
"""

INJECT_POOL = r"""
() => {
  const rows = [
    ['kzmplqwerxty@outlook.com', 'hte074521@wroutlook.cc', 'US-Residential-10x', '08-24 14:32', '2 小时', '0'],
    ['bnhfgrtwsxpl@outlook.com', 'mwp382910@wroutlook.cc', 'US-Residential-10x', '08-24 14:33', '2 小时', '0'],
    ['qwrtyplmnbvc@outlook.com', 'xkp119203@wroutlook.cc', 'US-Residential-5x', '08-24 11:06', '5 小时', '0'],
    ['plmnbvcxzaqw@outlook.com', 'zht445821@wroutlook.cc', 'SG-Test-3x', '08-23 22:19', '1 天', '1'],
    ['xzaqwertyuio@outlook.com', 'jkm882014@wroutlook.cc', 'US-Night-8x', '08-23 09:41', '2 天', '0'],
    ['mnbvcxzaqwpl@outlook.com', 'rwt589991@wroutlook.cc', 'US-Night-8x', '08-23 09:42', '2 天', '0'],
  ];
  const body = document.getElementById('poolBody');
  if (!body) return;
  body.innerHTML = rows.map(([em, rec, batch, created, age, relogin]) =>
    `<tr>
      <td><input type="checkbox" class="ck rowck"/></td>
      <td><span class="mono">${em}</span></td>
      <td>••••••••</td>
      <td><span class="mono" style="font-size:11px">${rec}</span></td>
      <td><span class="tok-main ok">✓ Graph 可读信</span></td>
      <td><span class="cell-batch">${batch}</span></td>
      <td class="cell-time">${created}</td>
      <td class="cell-age">${age}</td>
      <td class="cell-relogin mono">${relogin}</td>
      <td class="cell-time">08-24</td>
      <td><span class="rec-none">—</span></td>
      <td></td>
    </tr>`
  ).join('');
  const nav = document.getElementById('navPoolCount');
  if (nav) nav.textContent = '128';
  const page = document.getElementById('pageInfo');
  if (page) page.textContent = '第 1/22 页 · 共 128 条';
  const modal = document.getElementById('verifyModal');
  if (modal) modal.classList.remove('show');
}
"""

INJECT_PROXIES = r"""
() => {
  const cards = document.getElementById('proxyStatCards');
  if (cards) {
    const items = [
      ['总数', 6, 'var(--fg)'],
      ['启用', 6, 'var(--success)'],
      ['预检通过', 5, 'var(--success)'],
      ['已分配', 42, 'var(--info)'],
      ['成功率', '91%', 'var(--brand)'],
    ];
    cards.innerHTML = items.map(([l, v, c]) =>
      `<div class="card stat"><div class="lbl">${l}</div><div class="num" style="color:${c}">${v}</div></div>`
    ).join('');
  }
  const proxies = [
    ['US 住宅 #1', '代理商 A', 'US', '已配置', '是', 'ok', '—', '12/11/1'],
    ['US 住宅 #2', '代理商 A', 'US', '已配置', '是', 'ok', '—', '10/9/1'],
    ['US 住宅 #3', '代理商 A', 'US', '已配置', '是', 'ok', '—', '8/8/0'],
    ['US 旋转网关', '代理商 B', 'US', '已配置', '是', 'ok', '—', '7/7/0'],
    ['SG 静态', '代理商 C', 'SG', '已配置', '否', 'ok', '—', '5/5/0'],
    ['US 备用线', '代理商 B', 'US', '已配置', '是', 'err', '—', '0/0/0'],
  ];
  const body = document.getElementById('proxyBody');
  if (body) {
    body.innerHTML = proxies.map(([label, prov, country, tpl, sid, st, ip, stats]) =>
      `<tr>
        <td><input type="checkbox" class="ck proxy-row-ck"/></td>
        <td>${label}</td><td>${prov}</td><td>${country}</td>
        <td class="mono" style="font-size:11px;">${tpl}</td>
        <td><span class="badge brand">${sid === '是' ? '是' : '否'}</span></td>
        <td><span class="st ${st}">${st}</span></td>
        <td class="mono" style="font-size:11px;">${ip}</td>
        <td class="mono" style="font-size:11px;">${stats}</td>
        <td class="cell-time">08-24 14:00</td>
        <td><input type="checkbox" checked/></td>
        <td><button class="btn sm">预检</button></td>
      </tr>`
    ).join('');
  }
  const bind = document.getElementById('proxyBindBody');
  if (bind) {
    bind.innerHTML = [
      ['kzmplqwerxty@outlook.com', 'US 住宅 #1', 'register'],
      ['bnhfgrtwsxpl@outlook.com', 'US 住宅 #2', 'register'],
      ['qwrtyplmnbvc@outlook.com', 'US 旋转网关', 'rescue'],
    ].map(([em, label, purpose]) =>
      `<tr>
        <td class="mono">${em}</td>
        <td>${label}</td>
        <td class="mono" style="font-size:11px;">••••••••</td>
        <td>${purpose}</td>
        <td class="cell-time">2026-08-24 14:32:08</td>
        <td><button type="button" class="btn sm danger">解绑</button></td>
      </tr>`
    ).join('');
  }
  const nav = document.getElementById('navProxyCount');
  if (nav) nav.textContent = '6';
  const pf = document.getElementById('proxyPoolFile');
  if (pf) pf.textContent = 'sqlite · outlook.db';
}
"""

INJECT_BATCH_VERIFY = r"""
() => {
  const modal = document.getElementById('verifyModal');
  const sub = document.getElementById('verifyModalSub');
  const box = document.getElementById('verifyConsole');
  if (!modal || !box) return;
  modal.classList.add('show');
  if (sub) sub.textContent = '可用 112 / 124';
  const lines = [
    ['INFO', '开始测活 124 个（OAuth 换票默认直连，不走注册页代理）'],
    ['WARNING', '4 个没有 refresh_token，测不了，未列入'],
    ['INFO', '[1/124] kzmplqwerxty@outlook.com Graph 可读信'],
    ['INFO', '[2/124] bnhfgrtwsxpl@outlook.com Graph 可读信'],
    ['INFO', '[3/124] qwrtyplmnbvc@outlook.com Graph 可读信'],
    ['WARNING', '[47/124] plmnbvcxzaqw@outlook.com 网络异常，保留原状态'],
    ['INFO', '[48/124] xzaqwertyuio@outlook.com Graph 可读信'],
    ['INFO', '…'],
    ['ERROR', '[121/124] mnbvcxzaqwpl@outlook.com 已失活 AADSTS70000 service abuse mode'],
    ['INFO', '结束：可用 112 · 失败 8 · 网络跳过 4'],
  ];
  box.innerHTML = lines.map(([lv, msg], i) => {
    const ts = `14:3${5 + Math.floor(i / 2)}:${String((i * 7) % 60).padStart(2, '0')}`;
    return `<div class="l-${lv}"><span class="ts">${ts}</span>${msg}</div>`;
  }).join('');
  const sel = document.getElementById('selCount');
  if (sel) sel.innerHTML = '已选 <b>0</b>';
}
"""

SECTIONS = [
    ("概览", "overview", [INJECT_OVERVIEW]),
    ("批次注册", "register", [INJECT_REGISTER]),
    ("代理池", "proxies", [INJECT_PROXIES]),
    ("账号池", "pool", [INJECT_POOL]),
    ("测活", "pool", [INJECT_POOL, INJECT_BATCH_VERIFY]),
    ("IMAP保活", "ops", []),
]


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(BASE, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(1200)

        for name, sec, injectors in SECTIONS:
            await page.click(f'.nav[data-sec="{sec}"]')
            await page.wait_for_timeout(800)
            for js in injectors:
                await page.evaluate(js)
            await page.evaluate(SCRUB_JS)
            await page.wait_for_timeout(300)
            path = OUT / f"{name}.png"
            await page.screenshot(path=str(path), full_page=False)
            print(f"wrote {path}")
            await page.evaluate(
                "() => document.querySelectorAll('.modal-bg.show').forEach(m => m.classList.remove('show'))"
            )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
